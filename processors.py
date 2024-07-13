import re
from datetime import datetime
import sys
from typing import List

from async_timeout import timeout

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    TextFrame,
    UserStoppedSpeakingFrame,
    TranscriptionFrame,
    LLMMessagesFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.services.daily import DailyTransportMessageFrame
from pipecat.processors.aggregators.llm_response import LLMResponseAggregator

from utils.helpers import load_sounds
from loguru import logger
from prompts import IMAGE_GEN_PROMPT, CUE_USER_TURN, CUE_ASSISTANT_TURN

# sounds = load_sounds(["talking.wav", "listening.wav", "ding.wav"])

# -------------- Frame Types ------------- #


class StoryPageFrame(TextFrame):
    # Frame for each sentence in the story before a [break]
    pass


class StoryImageFrame(TextFrame):
    # Frame for trigger image generation
    pass


class StoryPromptFrame(TextFrame):
    # Frame for prompting the user for input
    pass


# ------------ Frame Processors ----------- #

class ConversationProcessor(FrameProcessor):
    """
    This frame processor keeps track of a conversation by capturing TranscriptionFrames
    and aggregating the text along with timestamps and user IDs in a conversation array.

    Attributes:
        conversation (list): A list of dictionaries containing conversation entries.
    """


    def __init__(self, messages: List[dict] = []):
        super().__init__()
        self._messages = messages
        self._aggregation = []
        self._role = "user"

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        logger.debug(f"ConversationProcessor: {frame}")

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Send an app message to the UI
            # await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))
            # await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))
            await self._push_aggregation()
        elif isinstance(frame, TranscriptionFrame):
            entry = {
                "user_id": frame.user_id,
                "text": frame.text,
                "timestamp": frame.timestamp
            }
            self._aggregation.append(entry)
        elif isinstance(frame, LLMMessagesFrame):
            # llm response
            logger.debug(f"LLM response: {frame.messages}")
        else:
            # Pass the frame along unchanged
            await self.push_frame(frame, direction)

    async def _push_aggregation(self):
        if len(self._aggregation) > 0:

            self._messages.append({"role": self._role, "content": self.format_aggregation()})

            # Reset the aggregation. Reset it before pushing it down, otherwise
            # if the tasks gets cancelled we won't be able to clear things up.
            self._aggregation = []

            frame = LLMMessagesFrame(self._messages)
            logger.debug(f"Pushing LLMMessagesFrame: {self._messages}")
            await self.push_frame(frame)

    def format_aggregation(self):
        """
        Formats the aggregation into a multi-line string.
        """
        formatted = []
        for entry in self._aggregation:
            formatted.append(f"{entry['timestamp']} - {entry['user_id']}: {entry['text']}")
        return "\n".join(formatted)

    def get_conversation_history(self):
        """
        Returns the entire conversation history.
        """
        return self._messages

    def get_last_n_entries(self, n):
        """
        Returns the last n entries of the conversation.
        """
        return self._messages[-n:]


class SentenceAggregator(FrameProcessor):
    """This frame processor aggregates text frames into complete sentences.

    Frame input/output:
        TextFrame("Hello,") -> None
        TextFrame(" world.") -> TextFrame("Hello world.")

    Doctest:
    >>> async def print_frames(aggregator, frame):
    ...     async for frame in aggregator.process_frame(frame):
    ...         print(frame.text)

    >>> aggregator = SentenceAggregator()
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello,")))
    >>> asyncio.run(print_frames(aggregator, TextFrame(" world.")))
    Hello, world.
    """

    def __init__(self):
        super().__init__()
        self._aggregation = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            m = re.search("(.*[?.!])(.*)", frame.text)
            if m:
                await self.push_frame(TextFrame(self._aggregation + m.group(1)))
                self._aggregation = m.group(2)
            else:
                self._aggregation += frame.text
        else:
            if self._aggregation:
                await self.push_frame(TextFrame(self._aggregation))
                self._aggregation = ""
            await self.push_frame(frame, direction)

class StoryImageProcessor(FrameProcessor):
    """
    Processor for image prompt frames that will be sent to the FAL service.

    This processor is responsible for consuming frames of type `StoryImageFrame`.
    It processes them by passing it to the FAL service.
    The processed frames are then yielded back.

    Attributes:
        _fal_service (FALService): The FAL service, generates the images (fast fast!).
    """

    def __init__(self, fal_service):
        super().__init__()
        self._fal_service = fal_service

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StoryImageFrame):
            try:
                async with timeout(7):
                    async for i in self._fal_service.run_image_gen(IMAGE_GEN_PROMPT % frame.text):
                        await self.push_frame(i)
            except TimeoutError:
                pass
            pass
        else:
            await self.push_frame(frame)


class StoryProcessor(FrameProcessor):
    """
    Primary frame processor. It takes the frames generated by the LLM
    and processes them into image prompts and story pages (sentences).
    For a clearer picture of how this works, reference prompts.py

    Attributes:
        _messages (list): A list of llm messages.
        _text (str): A buffer to store the text from text frames.
        _story (list): A list to store the story sentences, or 'pages'.

    Methods:
        process_frame: Processes a frame and removes any [break] or [image] tokens.
    """

    def __init__(self, messages, story):
        super().__init__()
        self._messages = messages
        self._text = ""
        self._story = story

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Send an app message to the UI
            await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))
            await self.push_frame(sounds["talking"])

        elif isinstance(frame, TextFrame):
            # We want to look for sentence breaks in the text
            # but since TextFrames are streamed from the LLM
            # we need to keep a buffer of the text we've seen so far
            self._text += frame.text

            # IMAGE PROMPT
            # Looking for: < [image prompt] > in the LLM response
            # We prompted our LLM to add an image prompt in the response
            # so we use regex matching to find it and yield a StoryImageFrame
            if re.search(r"<.*?>", self._text):
                if not re.search(r"<.*?>.*?>", self._text):
                    # Pass any frames until we have a closing bracket
                    # otherwise the image prompt will be passed to TTS
                    pass
                # Extract the image prompt from the text using regex
                image_prompt = re.search(r"<(.*?)>", self._text).group(1)
                # Remove the image prompt from the text
                self._text = re.sub(r"<.*?>", '', self._text, count=1)
                # Process the image prompt frame
                await self.push_frame(StoryImageFrame(image_prompt))

            # STORY PAGE
            # Looking for: [break] in the LLM response
            # We prompted our LLM to add a [break] after each sentence
            # so we use regex matching to find it in the LLM response
            if re.search(r".*\[[bB]reak\].*", self._text):
                # Remove the [break] token from the text
                # so it isn't spoken out loud by the TTS
                self._text = re.sub(r'\[[bB]reak\]', '',
                                    self._text, flags=re.IGNORECASE)
                self._text = self._text.replace("\n", " ")
                if len(self._text) > 2:
                    # Append the sentence to the story
                    self._story.append(self._text)
                    await self.push_frame(StoryPageFrame(self._text))
                    # Assert that it's the LLMs turn, until we're finished
                    await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))
                # Clear the buffer
                self._text = ""

        # End of a full LLM response
        # Driven by the prompt, the LLM should have asked the user for input
        elif isinstance(frame, LLMFullResponseEndFrame):
            # We use a different frame type, as to avoid image generation ingest
            await self.push_frame(StoryPromptFrame(self._text))
            self._text = ""
            await self.push_frame(frame)
            # Send an app message to the UI
            await self.push_frame(DailyTransportMessageFrame(CUE_USER_TURN))
            await self.push_frame(sounds["listening"])

        # Anything that is not a TextFrame pass through
        else:
            await self.push_frame(frame)
