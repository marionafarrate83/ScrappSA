import json
import csv
import uuid
import threading
import queue
import io
import os
from flask import Flask, request, Response, render_template, jsonify, stream_with_context

import scraper as sc
import price_scraper as ps
import maps_scraper as ms

app = Flask(__name__)

# job_id -> { "status": "running"|"done"|"error", "results": [...], "log": queue }
jobs: dict = {}
jobs_lock = threading.Lock()


def run_scrape_job(job_id: str, query: str, location: str):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = sc.scrape_all(query, location, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)  # sentinel


def run_price_job(job_id: str, query: str):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ps.compare_prices(query, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)  # sentinel


def run_maps_job(job_id: str, query: str, location: str, filters: dict):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ms.scrape_maps(query, location, filters=filters, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)  # sentinel


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def start_scrape():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()

    if not query or not location:
        return jsonify({"error": "Faltan parámetros query y location"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "query": query,
            "location": location,
            "results": [],
            "log": queue.Queue(),
        }

    thread = threading.Thread(
        target=run_scrape_job, args=(job_id, query, location), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/maps", methods=["POST"])
def start_maps():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()
    filters = data.get("filters") or {}

    if not query or not location:
        return jsonify({"error": "Faltan parámetros query y location"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "maps",
            "query": query,
            "location": location,
            "filters": filters,
            "results": [],
            "log": queue.Queue(),
        }

    thread = threading.Thread(
        target=run_maps_job, args=(job_id, query, location, filters), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/compare", methods=["POST"])
def start_compare():
    data = request.get_json()
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"error": "Falta parámetro query"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "prices",
            "query": query,
            "location": "",
            "results": [],
            "log": queue.Queue(),
        }

    thread = threading.Thread(target=run_price_job, args=(job_id, query), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def progress_stream(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404

    def generate():
        log_q = jobs[job_id]["log"]
        while True:
            try:
                msg = log_q.get(timeout=30)
            except queue.Empty:
                yield "data: ping\n\n"
                continue

            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

        # Send final status
        status = jobs[job_id].get("status", "done")
        results = jobs[job_id].get("results", [])
        if jobs[job_id].get("kind") == "prices":
            count = len([item for item in results if item.get("precio") is not None])
        else:
            count = len(results)
        yield f"data: {json.dumps({'__status__': status, '__count__': count})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/results/<job_id>")
def get_results(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404
    job = jobs[job_id]
    return jsonify({
        "status": job["status"],
        "kind": job.get("kind", "directory"),
        "query": job["query"],
        "location": job["location"],
        "count": len(job["results"]),
        "results": job["results"],
    })


@app.route("/api/download/<job_id>/<fmt>")
def download(job_id: str, fmt: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404

    job = jobs[job_id]
    results = job["results"]
    if job.get("kind") == "maps":
        slug = f"google_maps_{job['query']}_{job['location']}".replace(" ", "_")
    elif job.get("kind") == "prices":
        slug = f"comparador_{job['query']}".replace(" ", "_")
    else:
        slug = f"{job['query']}_{job['location']}".replace(" ", "_")

    if fmt == "json":
        content = json.dumps(results, ensure_ascii=False, indent=2)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{slug}.json"'},
        )

    if fmt == "csv":
        output = io.StringIO()
        if job.get("kind") == "maps":
            fields = [
                "nombre", "categoria", "calificacion", "resenas", "telefono",
                "direccion", "sitio_web", "google_maps_url", "latitud", "longitud",
                "linkedin_url", "lider_nombre", "lider_cargo", "lider_fuente",
                "lider_confianza", "estado"
            ]
        elif job.get("kind") == "prices":
            fields = ["tienda", "producto", "precio", "precio_texto", "url", "fuente", "estado"]
        else:
            fields = ["nombre", "telefono", "whatsapp", "direccion", "sitio_web", "email"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
        content = "﻿" + output.getvalue()  # UTF-8 BOM para Excel
        return Response(
            content,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'},
        )

    return jsonify({"error": "Formato inválido. Usa json o csv"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", debug=True, port=port, threaded=True)
