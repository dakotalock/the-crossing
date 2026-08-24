from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing.models import Agent, Principal, Task, new_id
from crossing.policy import PolicyDenied, Reason, check_agent


def create_principal(session: Session, name: str) -> Principal:
    p = Principal(id=new_id(), name=name)
    session.add(p)
    session.flush()
    return p


def create_agent(session: Session, principal_id: str, name: str, parent_id: str | None = None) -> Agent:
    if parent_id:
        parent = session.get(Agent, parent_id)
        check_agent(parent)
        if parent.principal_id != principal_id:
            raise PolicyDenied(Reason.PRINCIPAL_MISSING, "parent agent principal mismatch")
    a = Agent(id=new_id(), principal_id=principal_id, parent_id=parent_id, name=name, revoked=False)
    session.add(a)
    session.flush()
    return a


def revoke_agent(session: Session, agent_id: str) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise PolicyDenied(Reason.AGENT_REVOKED, "agent missing")
    agent.revoked = True
    kids = session.scalars(select(Agent).where(Agent.parent_id == agent_id)).all()
    stack = list(kids)
    while stack:
        child = stack.pop()
        child.revoked = True
        stack.extend(session.scalars(select(Agent).where(Agent.parent_id == child.id)).all())
    session.flush()
    return agent


def create_task(session: Session, principal_id: str, agent_id: str | None, name: str = "task") -> Task:
    t = Task(id=new_id(), principal_id=principal_id, agent_id=agent_id, name=name, status="open")
    session.add(t)
    session.flush()
    return t


def require_live_agent(session: Session, agent_id: str) -> Agent:
    agent = session.get(Agent, agent_id)
    check_agent(agent)
    if agent.parent_id:
        parent = session.get(Agent, agent.parent_id)
        check_agent(parent)
    return agent
