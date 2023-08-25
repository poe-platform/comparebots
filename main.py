import os

from fastapi_poe import make_app
from modal import Image, Secret, Stub, asgi_app

from comparebot import CompareBot

image = Image.debian_slim().pip_install_from_requirements("requirements.txt")
stub = Stub("comparebots")


@stub.function(image=image, secret=Secret.from_name("compare-bots-api-key"))
@asgi_app()
def fastapi_app():
    return make_app(CompareBot(), api_key=os.environ["POE_API_KEY"])
