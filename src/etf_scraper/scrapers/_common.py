"""Helpers shared by the provider scrapers."""

def _check_exp_provider(given, exp, class_name):
    if given != exp:
        raise ValueError(
            f"Provider should be {exp}, not {given} for class {class_name}"
        )
