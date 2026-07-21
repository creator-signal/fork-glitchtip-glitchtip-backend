import json
import os

import django


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
    django.setup()

    from creativesignal.zitadel.reconcile import ReconcileConfig, reconcile

    print(json.dumps(reconcile(ReconcileConfig.from_environment()), sort_keys=True))


if __name__ == "__main__":
    main()
