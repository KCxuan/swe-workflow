from typing import Literal
from pydantic import BaseModel, Field, model_validator


class Criterion(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: Literal["browser", "api", "data", "code"]


class RequirementPlan(BaseModel):
    title: str
    summary: str
    features: list[str] = Field(min_length=1)
    pages: list[str] = Field(min_length=1)
    tech_stack: str
    acceptance: list[Criterion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.acceptance]
        if len(ids) != len(set(ids)):
            raise ValueError("验收项 ID 必须唯一")
        return self


class CommandSpec(BaseModel):
    program: str
    args: list[str] = Field(default_factory=list)
    cwd: str = "."


class SetupCommand(CommandSpec):
    kind: Literal["dependency", "build"] = "dependency"


class ServiceSpec(CommandSpec):
    name: str
    port: int = Field(ge=1024, le=65535)
    health_path: str = "/"
    preview: bool = False


class RegisteredCheck(CommandSpec):
    id: str
    description: str


class CodingResult(BaseModel):
    summary: str
    changed_files: list[str]
    setup_commands: list[SetupCommand] = Field(default_factory=list)
    services: list[ServiceSpec] = Field(min_length=1)
    checks: list[RegisteredCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def service_ids(self):
        if len({s.port for s in self.services}) != len(self.services):
            raise ValueError("不同服务必须使用不同端口")
        if sum(s.preview for s in self.services) != 1:
            raise ValueError("必须有且仅有一个预览服务")
        if len({s.name for s in self.services}) != len(self.services):
            raise ValueError("服务名称不能重复")
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("检查任务 ID 不能重复")
        return self


class CheckResult(BaseModel):
    criterion_id: str
    status: Literal["passed", "failed", "not_run"]
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    summary: str
    results: list[CheckResult]
    issues: list[str] = Field(default_factory=list)


class RunInput(BaseModel):
    path: str
    mode: Literal["new", "modify"] = "new"
    request: str = Field(min_length=1, max_length=30000)


class DecisionInput(BaseModel):
    kind: Literal["confirm", "end"]
    plan: RequirementPlan | None = None
