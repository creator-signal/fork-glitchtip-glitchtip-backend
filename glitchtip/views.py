from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render


async def health(request):
    return HttpResponse("ok", content_type="text/plain")


def index(request, *args):
    return render(request, "index.html", {"base_path": settings.FORCE_SCRIPT_NAME})
