from enum import StrEnum
from pydantic import BaseModel, Field, model_validator
from typing import Self
from uuid import UUID, uuid4


class VerificationStatus(StrEnum):
    PASSED = "passed"
    REPAIRABLE = "repairable"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"


class Specialist(StrEnum):
    RAG_SPECIALIST = "rag_specialist"
    METADATA_SPECIALIST = "metadata_specialist"
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"


class RepairTask(BaseModel):
    target: Specialist
    task_id: UUID = Field(default_factory=uuid4)
    local_task_id: int
    task_name: str
    problem: str
    required_outcome: str
    depends_on: list[int] = Field(default_factory=list)


class VerificationResult(BaseModel):
    repair_tasks: list[RepairTask] = Field(default_factory=list)
    result: VerificationStatus
    result_justification: str

    @model_validator(mode="after")
    def validate_verification_result(self) -> Self:
        available_ids = {task.local_task_id 
                    for task in self.repair_tasks}
        dependency_ids = {local_id 
                        for task in self.repair_tasks 
                        for local_id in task.depends_on}
        missing_ids = dependency_ids - available_ids

        if missing_ids:
            raise ValueError(f"Dependencies reference missing task IDs: {sorted(missing_ids)}")
        if self.result == VerificationStatus.PASSED and self.repair_tasks:
            raise ValueError("Passed status cannot have repair tasks associated")
        elif self.result == VerificationStatus.REPAIRABLE and not self.repair_tasks:
            raise ValueError("Repairable status must have repair tasks associated")
        else:
            return self
        



