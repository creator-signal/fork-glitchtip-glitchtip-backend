from django.http import HttpResponse
import asyncio


async def health(request):
    await asyncio.sleep(32)
    return HttpResponse("ok", content_type="text/plain")
