"""
operations/service.py
========================
All DB reads/writes for the Operation model go through here, so the
executor/runners never touch SQLAlchemy directly and the locking rule
("one active operation of a given type per server") lives in one place.
"""
import logging
from datetime import datetime

from database.models import db, Operation, OperationStatus

logger = logging.getLogger(__name__)


class OperationConflict(Exception):
    def __init__(self, existing_operation: Operation):
        self.existing_operation = existing_operation
        super().__init__(
            f"Operation {existing_operation.operation_type} already "
            f"in progress for server {existing_operation.server_id} "
            f"(operation id {existing_operation.id})"
        )


def create_operation(server_id: int, operation_type: str, vendor: str) -> Operation:
    existing = (
        Operation.query
        .filter_by(server_id=server_id, operation_type=operation_type)
        .filter(Operation.status.in_([OperationStatus.QUEUED, OperationStatus.RUNNING]))
        .first()
    )
    if existing:
        raise OperationConflict(existing)

    op = Operation(
        server_id=server_id,
        operation_type=operation_type,
        vendor=vendor,
        status=OperationStatus.QUEUED,
    )
    db.session.add(op)
    db.session.commit()
    logger.info("Created operation %s (%s) for server %s", op.id, operation_type, server_id)
    return op


def get_operation(operation_id: int) -> Operation | None:
    return Operation.query.get(operation_id)


def mark_running(operation_id: int):
    op = Operation.query.get(operation_id)
    if not op:
        return
    op.status = OperationStatus.RUNNING
    op.started_at = datetime.utcnow()
    db.session.commit()


def update_progress(operation_id: int, percent, message: str = None):
    op = Operation.query.get(operation_id)
    if not op:
        return
    if percent is not None:
        op.progress_percent = percent
    if message is not None:
        op.status_message = message
    db.session.commit()


def mark_completed(operation_id: int, result_path: str = None, result_filename: str = None,
                    result_content_type: str = None, message: str = "Completed"):
    op = Operation.query.get(operation_id)
    if not op:
        return
    op.status = OperationStatus.COMPLETED
    op.progress_percent = 100
    op.status_message = message
    op.result_path = result_path
    op.result_filename = result_filename
    op.result_content_type = result_content_type
    op.completed_at = datetime.utcnow()
    db.session.commit()
    logger.info("Operation %s completed", operation_id)


def mark_failed(operation_id: int, error_message: str):
    op = Operation.query.get(operation_id)
    if not op:
        return
    op.status = OperationStatus.FAILED
    op.error_message = error_message
    op.completed_at = datetime.utcnow()
    db.session.commit()
    logger.error("Operation %s failed: %s", operation_id, error_message)


def reconcile_orphaned_operations():
    try:
        orphaned = Operation.query.filter(
            Operation.status.in_([OperationStatus.QUEUED, OperationStatus.RUNNING])
        ).all()
    except Exception:
        # Table may not exist yet (fresh DB before migrations)
        db.session.rollback()
        return
    for op in orphaned:
        op.status = OperationStatus.FAILED
        op.error_message = "Interrupted by application restart"
        op.completed_at = datetime.utcnow()
    if orphaned:
        db.session.commit()
        logger.warning("Reconciled %d orphaned operation(s) on startup", len(orphaned))