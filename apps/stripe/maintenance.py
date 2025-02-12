from .models import StripeProduct, StripeSubscription


async def sync_stripe_models():
    await StripeProduct.sync_from_stripe()
    await StripeSubscription.sync_from_stripe()
