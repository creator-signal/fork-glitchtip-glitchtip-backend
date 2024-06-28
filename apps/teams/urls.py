from django.urls import include, path
from rest_framework_nested import routers

from glitchtip.routers import BulkSimpleRouter

from .views import TeamViewSet

router = BulkSimpleRouter()
router.register(r"teams", TeamViewSet)

teams_router = routers.NestedSimpleRouter(router, r"teams", lookup="team")

urlpatterns = [path("", include(router.urls)), path("", include(teams_router.urls))]
