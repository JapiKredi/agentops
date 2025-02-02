import inspect
import pprint
from typing import Optional

from agentops.llms.providers.instrumented_provider import InstrumentedProvider
from agentops.time_travel import fetch_completion_override_from_time_travel_cache

from agentops.event import ActionEvent, ErrorEvent, LLMEvent
from agentops.session import Session
from agentops.log_config import logger
from agentops.helpers import check_call_stack_for_agent_id, get_ISO_time
from agentops.singleton import singleton


@singleton
class OpenAiProvider(InstrumentedProvider):
    original_create = None
    original_create_async = None

    def __init__(self, client):
        super().__init__(client)
        self._provider_name = "OpenAI"

    def handle_response(self, response, kwargs, init_timestamp, session: Optional[Session] = None) -> dict:
        """Handle responses for OpenAI versions >v1.0.0"""
        from openai import AsyncStream, Stream
        from openai.resources import AsyncCompletions
        from openai.types.chat import ChatCompletionChunk

        llm_event = LLMEvent(init_timestamp=init_timestamp, params=kwargs)
        if session is not None:
            llm_event.session_id = session.session_id

        def handle_stream_chunk(chunk: ChatCompletionChunk):
            # NOTE: prompt/completion usage not returned in response when streaming
            # We take the first ChatCompletionChunk and accumulate the deltas from all subsequent chunks to build one full chat completion
            if llm_event.returns == None:
                llm_event.returns = chunk

            try:
                accumulated_delta = llm_event.returns.choices[0].delta
                llm_event.agent_id = check_call_stack_for_agent_id()
                llm_event.model = chunk.model
                llm_event.prompt = kwargs["messages"]

                # NOTE: We assume for completion only choices[0] is relevant
                choice = chunk.choices[0]

                if choice.delta.content:
                    accumulated_delta.content += choice.delta.content

                if choice.delta.role:
                    accumulated_delta.role = choice.delta.role

                if choice.delta.tool_calls:
                    accumulated_delta.tool_calls = choice.delta.tool_calls

                if choice.delta.function_call:
                    accumulated_delta.function_call = choice.delta.function_call

                if choice.finish_reason:
                    # Streaming is done. Record LLMEvent
                    llm_event.returns.choices[0].finish_reason = choice.finish_reason
                    llm_event.completion = {
                        "role": accumulated_delta.role,
                        "content": accumulated_delta.content,
                        "function_call": accumulated_delta.function_call,
                        "tool_calls": accumulated_delta.tool_calls,
                    }
                    llm_event.end_timestamp = get_ISO_time()

                    self._safe_record(session, llm_event)
            except Exception as e:
                self._safe_record(session, ErrorEvent(trigger_event=llm_event, exception=e))

                kwargs_str = pprint.pformat(kwargs)
                chunk = pprint.pformat(chunk)
                logger.warning(
                    f"Unable to parse a chunk for LLM call. Skipping upload to AgentOps\n"
                    f"chunk:\n {chunk}\n"
                    f"kwargs:\n {kwargs_str}\n"
                )

        # if the response is a generator, decorate the generator
        if isinstance(response, Stream):

            def generator():
                for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return generator()

        # For asynchronous AsyncStream
        elif isinstance(response, AsyncStream):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # For async AsyncCompletion
        elif isinstance(response, AsyncCompletions):

            async def async_generator():
                async for chunk in response:
                    handle_stream_chunk(chunk)
                    yield chunk

            return async_generator()

        # v1.0.0+ responses are objects
        try:
            llm_event.returns = response
            llm_event.agent_id = check_call_stack_for_agent_id()
            llm_event.prompt = kwargs["messages"]
            llm_event.prompt_tokens = response.usage.prompt_tokens
            llm_event.completion = response.choices[0].message.model_dump()
            llm_event.completion_tokens = response.usage.completion_tokens
            llm_event.model = response.model

            self._safe_record(session, llm_event)
        except Exception as e:
            self._safe_record(session, ErrorEvent(trigger_event=llm_event, exception=e))

            kwargs_str = pprint.pformat(kwargs)
            response = pprint.pformat(response)
            logger.warning(
                f"Unable to parse response for LLM call. Skipping upload to AgentOps\n"
                f"response:\n {response}\n"
                f"kwargs:\n {kwargs_str}\n"
            )

        return response

    def override(self):
        self._override_openai_v1_completion()
        self._override_openai_v1_async_completion()

    def _override_openai_v1_completion(self):
        from openai.resources.chat import completions
        from openai.types.chat import ChatCompletion, ChatCompletionChunk

        # Store the original method
        self.original_create = completions.Completions.create

        def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()
            session = kwargs.get("session", None)
            if "session" in kwargs.keys():
                del kwargs["session"]

            completion_override = fetch_completion_override_from_time_travel_cache(kwargs)
            if completion_override:
                result_model = None
                pydantic_models = (ChatCompletion, ChatCompletionChunk)
                for pydantic_model in pydantic_models:
                    try:
                        result_model = pydantic_model.model_validate_json(completion_override)
                        break
                    except Exception as e:
                        pass

                if result_model is None:
                    logger.error(
                        f"Time Travel: Pydantic validation failed for {pydantic_models} \n"
                        f"Time Travel: Completion override was:\n"
                        f"{pprint.pformat(completion_override)}"
                    )
                    return None
                return self.handle_response(result_model, kwargs, init_timestamp, session=session)

            # prompt_override = fetch_prompt_override_from_time_travel_cache(kwargs)
            # if prompt_override:
            #     kwargs["messages"] = prompt_override["messages"]

            # Call the original function with its original arguments
            result = self.original_create(*args, **kwargs)
            return self.handle_response(result, kwargs, init_timestamp, session=session)

        # Override the original method with the patched one
        completions.Completions.create = patched_function

    def _override_openai_v1_async_completion(self):
        from openai.resources.chat import completions
        from openai.types.chat import ChatCompletion, ChatCompletionChunk

        # Store the original method
        self.original_create_async = completions.AsyncCompletions.create

        async def patched_function(*args, **kwargs):
            init_timestamp = get_ISO_time()

            session = kwargs.get("session", None)
            if "session" in kwargs.keys():
                del kwargs["session"]

            completion_override = fetch_completion_override_from_time_travel_cache(kwargs)
            if completion_override:
                result_model = None
                pydantic_models = (ChatCompletion, ChatCompletionChunk)
                for pydantic_model in pydantic_models:
                    try:
                        result_model = pydantic_model.model_validate_json(completion_override)
                        break
                    except Exception as e:
                        pass

                if result_model is None:
                    logger.error(
                        f"Time Travel: Pydantic validation failed for {pydantic_models} \n"
                        f"Time Travel: Completion override was:\n"
                        f"{pprint.pformat(completion_override)}"
                    )
                    return None
                return self.handle_response(result_model, kwargs, init_timestamp, session=session)

            # prompt_override = fetch_prompt_override_from_time_travel_cache(kwargs)
            # if prompt_override:
            #     kwargs["messages"] = prompt_override["messages"]

            # Call the original function with its original arguments
            result = await self.original_create_async(*args, **kwargs)
            return self.handle_response(result, kwargs, init_timestamp, session=session)

        # Override the original method with the patched one
        completions.AsyncCompletions.create = patched_function

    def undo_override(self):
        if self.original_create is not None and self.original_create_async is not None:
            from openai.resources.chat import completions

            completions.AsyncCompletions.create = self.original_create_async
            completions.Completions.create = self.original_create
