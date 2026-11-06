import asyncio
import logging
import ssl
import threading
from aiohttp import web
from .app import App
from .utils import DATA_DIR, FRONTEND, HTTP_PORT, HTTPS_PORT


def create_app(state=None):
    state = state if state is not None else App()
    app = web.Application(client_max_size=1_500_000)
    app["state"] = state
    app.router.add_get("/api/{path:.*}", state.get)
    app.router.add_post("/api/{path:.*}", state.action)
    app.router.add_get("/camera/{name}.mjpg", state.stream)
    app.router.add_get("/ws", state.websocket)
    app.router.add_get("/camera-ws", state.camera_socket)
    app.router.add_get("/episodes/{name}", state.download)

    async def index(request):
        return web.FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-store"})

    async def cache_control(request, response):
        response.headers["Cache-Control"] = "no-store"

    app.on_response_prepare.append(cache_control)
    app.router.add_get("/", index)
    app.router.add_static("/static/", FRONTEND, show_index=False) 
    return app


async def main():
    state = App()
    runner = web.AppRunner(create_app(state))
    worker = None
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
        certificate, key = DATA_DIR / "tls/cert.pem", DATA_DIR / "tls/key.pem"
        if certificate.exists() and key.exists():
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(certificate, key)
            await web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=tls).start()
        else:
            logging.warning(
                "HTTPS inactive: add cert.pem and key.pem to %s/tls for Quest VR", DATA_DIR
            )
        worker = threading.Thread(target=state.motor.loop, daemon=True)
        worker.start()
        state.cameras.start()
        logging.info("UMI ready on port %s; data: %s", HTTP_PORT, DATA_DIR)
        await asyncio.Event().wait()
    finally:
        state.motor.running = False
        if worker is not None:
            await asyncio.to_thread(worker.join, 2)
        state.motor.close()
        state.cameras.close()
        state.recorder.stop("not judged", "Application stopped", "system")
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
