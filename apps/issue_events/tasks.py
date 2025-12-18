from django.tasks import task

from .models import Issue


@task
def delete_issue_task(ids: list[int]):
    for id in ids:
        Issue.objects.get(id=id).force_delete()
