import pathlib

p = pathlib.Path('hydra_ws_server.py')
text = p.read_text(encoding='utf-8')
text = text.replace('def __init__(self, host: str = "127.0.0.1"', 'def __init__(self, host: str = "0.0.0.0"')
p.write_text(text, encoding='utf-8')
