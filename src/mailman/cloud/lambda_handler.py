"""ep-api Lambda entry point - wraps FastAPI app with Mangum."""
from mangum import Mangum
from mailman.cloud.main import app

handler = Mangum(app, lifespan="off")
