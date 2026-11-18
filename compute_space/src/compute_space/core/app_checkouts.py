import os


def app_checkout_dir(temp_data_dir: str, app_name: str) -> str:
    """Host-only source directory, outside the app data mounts."""
    return os.path.join(temp_data_dir, "app_checkouts", app_name)
