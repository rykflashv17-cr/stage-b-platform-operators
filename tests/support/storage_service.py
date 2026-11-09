"""Four fresh Pipeline processes, connected only by Storage-signed files."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from uuid import UUID
from zipfile import ZipFile

import grpc
import pytest
from operator_sdk._proto import storage_contract_pb2 as pb
from operator_sdk._proto import storage_contract_pb2_grpc as rpc
from operator_sdk.pipeline_storage import Storage


@pytest.fixture
def remote():
    objects, calls, consumer_grants = {}, [], set()
    class HTTP(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            key = self.path[1:]
            if key not in objects or self.headers.get("X-Grant") != "fixture":
                self.send_error(403)
                return
            body = objects[key]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_PUT(self):
            if self.headers.get("X-Grant") != "fixture":
                self.send_error(403)
                return
            objects[self.path[1:]] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
    http = ThreadingHTTPServer(("127.0.0.1",0), HTTP)
    client_host = os.getenv("MCAP_TEST_STORAGE_HOST", "127.0.0.1")
    address = f"http://{client_host}:{http.server_port}"
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    class Signer(rpc.StorageExecutionServiceServicer):
        def sign(self, req, call):
            assert req.service_code == "data-platform-operator-runtime"
            key = req.items[0].object_key
            result = pb.SignResponse()
            signing_address = address
            if req.service_credential == "delivery-consumer":
                if req.operation != "DOWNLOAD" or (req.storage_id, key) not in consumer_grants:
                    call.set_trailing_metadata((("x-error-code", "denied"),))
                    return result
                signing_address = f"http://127.0.0.1:{http.server_port}"
            else:
                assert req.service_credential == "fixture-only"
            calls.append((req.operation, key))
            if req.operation == "UPLOAD" and key.endswith(".zip") and "deny" in objects:
                call.set_trailing_metadata((("x-error-code", "denied"),))
                return result
            result.auth_info.mode = "presigned_url"
            result.storage_info.endpoint = signing_address
            result.storage_info.provider = "S3"
            result.storage_info.bucket = req.storage_id
            item = result.items.add(object_key=key,presigned_url=signing_address+"/"+key)
            item.required_headers["X-Grant"] = "fixture"
            return result
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    rpc.add_StorageExecutionServiceServicer_to_server(Signer(),server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"{client_host}:{port}", objects, calls, consumer_grants
    finally:
        server.stop(0).wait()
        http.shutdown()
        http.server_close()
        thread.join()


def location(name):
    return dict(storageLocationId=name, bucket=name, rootPrefix=name, provider="S3")
