"""vLLM(OpenAI 호환) 채팅 모델.

`ChatOpenAI`는 OpenAI 공식 필드만 다루므로 vLLM이 보내는 `reasoning_content`(Thinking)를 버린다.
응답의 `reasoning_content`를 `additional_kwargs`에 보존한다(시각화용).

다음 요청에는 되돌려 보내지 않는다. `ChatOpenAI`의 메시지 변환이 이 필드를 빼므로 그대로 둔다.
seed32-think(vLLM)에서는 이전 턴의 reasoning_content를 다시 보내면 스트리밍 응답의 Thinking이
`</think>`와 함께 content로 새어 나온다 (도구 결과 뒤 호출에서 재현, 보내지 않으면 정상).
"""

import openai
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


class ChatVLLM(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        gen = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or []
        if gen is not None and choices and (delta := choices[0].get("delta")):
            if reasoning := delta.get("reasoning_content"):
                gen.message.additional_kwargs["reasoning_content"] = reasoning
        return gen

    def _create_chat_result(self, response: dict | openai.BaseModel, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        for gen, choice in zip(result.generations, response_dict.get("choices") or []):
            if reasoning := (choice.get("message") or {}).get("reasoning_content"):
                gen.message.additional_kwargs["reasoning_content"] = reasoning
        return result
