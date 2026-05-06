#!/usr/bin/env python3
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

GH = "http://localhost:2027"

app = FastAPI()


@app.post("/route")
async def route(request: Request):
    body = await request.body()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{GH}/route", content=body,
                              headers={"Content-Type": "application/json"})
    return Response(content=r.content, status_code=r.status_code,
                    media_type="application/json")
