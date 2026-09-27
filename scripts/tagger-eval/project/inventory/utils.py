def parse_price(text):
    text = text.strip()
    if not text:
        raise ValueError("empty value")
    if text.startswith("£"):
        text = text[1:]
    return float(text)


def parse_quantity(text):
    text = text.strip()
    if not text:
        raise ValueError("empty value")
    value = int(text)
    if value < 0:
        raise ValueError("negative quantity")
    return value


def parse_name(text):
    text = text.strip()
    if not text:
        raise ValueError("empty value")
    return text.lower()
