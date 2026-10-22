"""The real CrewAI agents, wired to a fake LLM: checks agents, tasks and structured output parse."""

import json

from crewai.llms.base_llm import BaseLLM

from app import llm as llm_module
from app.agents import CrewRunner
from tests.fakes import email


class ScriptedLLM(BaseLLM):
    answer: str = ""

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None, **kwargs):
        if response_model is not None:
            return response_model.model_validate_json(self.answer)
        return self.answer

    def supports_function_calling(self) -> bool:
        return False


def test_intake_and_cover_agents_parse_structured_output(monkeypatch):
    intake = {"email_class": "quote_request", "confidence": "high", "reason": "asks for prices",
              "quote_reference": None, "suspicious_instructions": False}
    monkeypatch.setattr(llm_module, "crew_llm", lambda: ScriptedLLM(model="scripted", answer=json.dumps(intake)))
    runner = CrewRunner()
    step = runner.classify(email(body="Please quote {braces} and {more}"))
    assert step.value.email_class == "quote_request"
    assert step.calls >= 1

    cover = {"subject": "Quotation Q-1", "body": "Dear Dave, please find it attached."}
    monkeypatch.setattr(llm_module, "crew_llm", lambda: ScriptedLLM(model="scripted", answer=json.dumps(cover)))
    note = runner.cover({"quote_ref": "Q-1", "not_quoted": ["{weird}"]}).value
    assert note.subject == "Quotation Q-1"
