from pydantic import BaseModel, Field
from typing import List, Optional

class ProposedAction(BaseModel):
    type: str = Field(description="Action type: restart_service, toggle_feature_flag, throttle_load_generator, scale_service")
    target: str = Field(description="The specific microservice or flag name")
    parameter: Optional[str] = Field(None, description="Additional parameters like replica count")
    reason: str

class Evidence(BaseModel):
    source: str = Field(description="trace, log, metric, container_health, deploy_history")
    reference: Optional[str] = Field(None, description="Trace ID, metric name, or timestamp")
    excerpt: str = Field(description="Contiguous text copied verbatim from tool output")

class Diagnosis(BaseModel):
    root_cause_service: str
    root_cause: str
    confidence: float
    evidence: List[Evidence]
    proposed_action: Optional[ProposedAction] = None
    reasoning_summary: str

class ToolLog(BaseModel):
    step: int
    name: str
    arguments: dict
    output_preview: str

class AgentResult(BaseModel):
    status: str
    diagnosis: Optional[Diagnosis] = None
    evidence_verified: bool
    findings_summary: str
    steps: int
    tool_calls: List[ToolLog]
    escalation_reason: Optional[str] = None

    def to_official(self) -> dict:
        out = {"status": self.status, "summary": self.findings_summary}
        if self.diagnosis:
            out["diagnosis"] = self.diagnosis.model_dump()
        if self.escalation_reason:
            out["escalation"] = self.escalation_reason
        return out