"""
Custom task backends for testing that support vtasks features.
"""

from django.conf import settings
from django.tasks.backends.immediate import ImmediateBackend
from django.tasks.base import TaskResult, TaskResultStatus
from django.utils import timezone
from django.utils.crypto import get_random_string


class VtasksImmediateBackend(ImmediateBackend):
    """
    Extended ImmediateBackend that accepts vtasks batch queue names.

    This backend runs tasks immediately (synchronously) like ImmediateBackend,
    but also accepts batch queue names from VTASKS_BATCH_QUEUES. This allows
    tests to use batch queue decorators without needing actual queue infrastructure.

    For batch queue tasks, it wraps the single task call in the batch format that
    the task function expects (a list of task dictionaries).
    """

    def __init__(self, alias, params):
        """Initialize backend and add batch queues to the queues list."""
        super().__init__(alias, params)
        # Add batch queue names to the accepted queues
        batch_queues = getattr(settings, "VTASKS_BATCH_QUEUES", {})
        self.queues = self.queues | set(batch_queues.keys())

    def enqueue(self, task, args, kwargs):
        """
        Enqueue a task, wrapping it in batch format if it's a batch queue task.
        """
        self.validate_task(task)

        # Check if this is a batch queue task
        batch_queues = getattr(settings, "VTASKS_BATCH_QUEUES", {})
        is_batch_queue = task.queue_name in batch_queues

        if is_batch_queue:
            # Wrap the single task in batch format: [{"args": args, "kwargs": kwargs}]
            batch_tasks = [{"args": args, "kwargs": kwargs}]

            # Create task result for the batch
            task_result = TaskResult(
                task=task,
                id=get_random_string(32),
                status=TaskResultStatus.READY,
                enqueued_at=None,
                started_at=None,
                last_attempted_at=None,
                finished_at=None,
                args=(batch_tasks,),  # Pass the batch as a single argument
                kwargs={},
                backend=self.alias,
                errors=[],
                worker_ids=[],
            )

            self._execute_task(task_result)
            return task_result
        else:
            # For non-batch tasks, use the standard immediate execution
            return super().enqueue(task, args, kwargs)
