"""Snapshot the current administrator-configured policy for a newly issued Key."""
import time


def new_key_defaults(settings):
    # Keep a reference to live Settings at the caller, not a startup snapshot.
    fields = ('daily_images', 'daily_anlas', 'daily_v5', 'monthly_anlas',
              'daily_text_tokens', 'rpm', 'allow_anlas', 'allow_img2img',
              'exclude_global_v5', 'image_model_scope')
    values = {name: getattr(settings, 'default_' + name) for name in fields}
    values['expires_at'] = (time.time() + settings.default_expires_days * 86400
                            if settings.default_expires_days > 0 else None)
    return values
