from django.urls import include, path
from rest_framework_nested import routers

from .views import ProductViewSet, SubscriptionViewSet

router = routers.SimpleRouter()
router.register(r"subscriptions", SubscriptionViewSet)
router.register(r"products", ProductViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
