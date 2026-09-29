"""PostgreSQL persistent store - SQLAlchemy models for the schema from the
M3 docx Week 2: tables for queries, responses, user_feedback, audit_logs,
analysis_runs, forecasts. Local Postgres only - see docker-compose.yml at
the repo root. Migrations are managed with Alembic (see migrations/).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://finops:finops_dev_only@localhost:5432/finops"


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QueryRecord(Base):
    """One user/API request - the root of a run's audit trail."""

    __tablename__ = "queries"

    id = Column(Integer, primary_key=True)
    session_id = Column(String, nullable=True)
    request_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    responses = relationship("ResponseRecord", back_populates="query", cascade="all, delete-orphan")


class ResponseRecord(Base):
    """One agent response to a query - intent, finding, confidence, which
    model produced it, and how long it took."""

    __tablename__ = "responses"

    id = Column(Integer, primary_key=True)
    query_id = Column(Integer, ForeignKey("queries.id"), nullable=False)
    intent = Column(String, nullable=True)
    finding = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    model_used = Column(String, nullable=True)
    latency_seconds = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    query = relationship("QueryRecord", back_populates="responses")
    feedback = relationship("UserFeedbackRecord", back_populates="response", cascade="all, delete-orphan")


class UserFeedbackRecord(Base):
    """A human's HITL decision on one response - approve/reject/edit, with
    an optional rationale. Feeds M3 Week 7's analytics (approval rate,
    accuracy, latency by model)."""

    __tablename__ = "user_feedback"

    id = Column(Integer, primary_key=True)
    response_id = Column(Integer, ForeignKey("responses.id"), nullable=False)
    decision = Column(String, nullable=False)  # approved / rejected / edited
    rationale = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    response = relationship("ResponseRecord", back_populates="feedback")


class AuditLogRecord(Base):
    """Append-only audit trail - every notable event, independent of the
    query/response/feedback chain (e.g. a rejected file, a denied MCP call)."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AnalysisRunRecord(Base):
    """One M3 analysis run - KPIs/anomalies/etc. computed for a batch, with
    the config snapshot that produced it (docx Week 4: 'run_id, timestamp,
    config snapshot, and output')."""

    __tablename__ = "analysis_runs"

    id = Column(Integer, primary_key=True)
    run_id = Column(String, nullable=False, unique=True)
    config_snapshot = Column(Text, nullable=True)
    output_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    forecasts = relationship("ForecastRecord", back_populates="analysis_run", cascade="all, delete-orphan")


class ForecastRecord(Base):
    """One forecast result tied to an analysis run (docx Week 6: 'store
    forecast results and assumptions in PostgreSQL for versioning')."""

    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True)
    analysis_run_id = Column(Integer, ForeignKey("analysis_runs.id"), nullable=True)
    service = Column(String, nullable=True)
    horizon_days = Column(Integer, nullable=False)
    forecast_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    analysis_run = relationship("AnalysisRunRecord", back_populates="forecasts")


def get_database_url() -> str:
    """SQLAlchemy-style URL (postgresql+psycopg://...), for create_engine()."""
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_psycopg_conninfo() -> str:
    """Plain libpq connection string, for callers that talk to psycopg
    directly rather than through SQLAlchemy - e.g. LangGraph's PostgresSaver,
    which calls psycopg.Connection.connect() itself and doesn't understand
    SQLAlchemy's '+psycopg' driver suffix in the URL scheme."""
    return get_database_url().replace("postgresql+psycopg://", "postgresql://", 1)


def get_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url(), future=True)


def get_session_factory(engine: Engine | None = None) -> sessionmaker:
    return sessionmaker(bind=engine or get_engine(), future=True, expire_on_commit=False)
