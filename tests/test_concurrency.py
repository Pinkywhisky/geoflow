"""A single ASGI loop must keep serving while ODA/inspection is blocked."""

import asyncio
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import threading

import anyio
import httpx
import pytest
from fastapi import Request, UploadFile

import app.concurrency as concurrency
import app.main as main
from app.domain import ControleTechnique, Planche
from app.profiles.models import DrawingProfile
from app.quick_analysis import QuickAnalysisRepository
from app.storage import JsonDossierRepository
from app.workflow import create_dossier


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "repository", JsonDossierRepository(tmp_path / "data"))
    monkeypatch.setattr(main, "analysis_repository", QuickAnalysisRepository(tmp_path / "analyses"))
    monkeypatch.setattr(concurrency, "analysis_limiter", anyio.CapacityLimiter(1))
    monkeypatch.setattr(concurrency, "admission_limiter", anyio.CapacityLimiter(2))
    dossier = create_dossier("CONCURRENCY", "copropriete")
    main.repository.save(dossier)
    return dossier


def block_stage(monkeypatch, stage, entered, release):
    def block():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release analysis")

    @contextmanager
    def source(path, file_type):
        assert Path(path).exists()
        if stage == "oda":
            assert file_type == "dwg"
            block()
        yield path

    def inspect(path, filename):
        assert Path(path).exists()
        if stage == "inspect":
            block()
        return (
            ControleTechnique(version_dxf="AC1032", unite_detectee="metre"),
            [Planche(id="model", titre="Model", methode_detection="layout_dxf")],
        )

    monkeypatch.setattr(main, "dxf_source", source)
    monkeypatch.setattr(main, "inspect_dxf", inspect)


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["oda", "inspect"])
@pytest.mark.parametrize("second_route", ["import", "profile-analysis", "profile-test"])
async def test_health_stays_responsive_and_analysis_is_serialized(storage, monkeypatch, stage, second_route):
    entered = threading.Event()
    release = threading.Event()
    block_stage(monkeypatch, stage, entered, release)
    if second_route == "import":
        path = f"/dossiers/{storage.id}/import"
    elif second_route == "profile-test":
        profile = main.profile_repository.save(DrawingProfile(name="Synthetic"))
        path = f"/profils/{profile.id}/tester"
    else:
        path = "/profils/analyse"
    suffix = "dwg" if stage == "oda" else "dxf"
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = asyncio.create_task(client.post("/analyze", files={"file": (f"first.{suffix}", b"synthetic")}))
        second = None
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.005)
            second = asyncio.create_task(client.post(path, files={"file": (f"second.{suffix}", b"synthetic")}))
            with anyio.fail_after(0.5):
                health = await client.get("/health")
            assert health.json() == {"status": "ok"}
            await anyio.sleep(0.02)
            limiter = concurrency.analysis_limiter
            assert limiter.borrowed_tokens == 1
            assert limiter.statistics().tasks_waiting == 1
            assert not first.done() and not second.done()
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert all(response.status_code == 303 for response in responses)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["autosave", "delete"])
async def test_import_does_not_overwrite_edits_or_recreate_a_deleted_dossier(storage, monkeypatch, change):
    entered, release = threading.Event(), threading.Event()
    block_stage(monkeypatch, "inspect", entered, release)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        upload = asyncio.create_task(client.post(
            f"/dossiers/{storage.id}/import", files={"file": ("plan.dxf", b"synthetic")},
        ))
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.005)
            if change == "delete":
                assert (await client.post(f"/dossiers/{storage.id}/delete")).status_code == 303
            else:
                assert (await client.post(
                    f"/dossiers/{storage.id}/reference", data={"reference": "EDITED"},
                )).status_code == 303
        finally:
            release.set()
            response = await asyncio.wait_for(upload, timeout=5)
        assert response.status_code == (404 if change == "delete" else 409)
        if change == "delete":
            assert main.repository.list() == []
        else:
            assert main.repository.get(storage.id).reference == "EDITED"
            assert main.repository.get(storage.id).plan_importe is None


@pytest.mark.anyio
async def test_cancelled_upload_removes_partial_temporary_file(tmp_path, monkeypatch):
    upload = UploadFile(filename="plan.dxf", file=BytesIO(b"synthetic"))
    original_factory = main.tempfile.NamedTemporaryFile
    paths = []

    def temporary(**kwargs):
        result = original_factory(dir=tmp_path, **kwargs)
        paths.append(Path(result.name))
        return result

    reads = 0

    async def read(size):
        nonlocal reads
        reads += 1
        if reads == 1:
            return b"partial upload"
        raise asyncio.CancelledError

    monkeypatch.setattr(main.tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(upload, "read", read)
    try:
        with pytest.raises(asyncio.CancelledError):
            await main._save_temporary_upload(upload, ".dxf")
    finally:
        await upload.close()
    assert len(paths) == 1
    assert not paths[0].exists()


@pytest.mark.anyio
async def test_profile_analysis_does_not_restore_a_deleted_snapshot(storage, monkeypatch):
    block_stage(monkeypatch, "none", threading.Event(), threading.Event())
    entered, release = threading.Event(), threading.Event()

    def slow_profile(snapshot, usage):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release profile analysis")
        main.apply_profile_choice(snapshot.dossier, usage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
    ) as client:
        uploaded = await client.post("/analyze", files={"file": ("plan.dxf", b"synthetic")})
        analysis_id = uploaded.headers["location"].split("/")[2]
        monkeypatch.setattr(main, "_apply_analysis_profile", slow_profile)
        analysis = asyncio.create_task(client.post(
            f"/analyses/{analysis_id}/profil", data={"profile": "legacy"},
        ))
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.005)
            deleted = await client.post(f"/analyses/{analysis_id}/delete")
            assert deleted.status_code == 303
        finally:
            release.set()
            response = await asyncio.wait_for(analysis, timeout=5)
        assert response.status_code == 404
        assert not main.analysis_repository._path(analysis_id).exists()


@pytest.mark.anyio
async def test_third_analysis_is_refused_before_reading_upload_body(storage, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    block_stage(monkeypatch, "inspect", entered, release)
    body_reads = []

    async def body():
        body_reads.append(True)
        yield b"must never be consumed"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
    ) as client:
        first = asyncio.create_task(client.post(
            "/analyze", files={"file": ("first.dxf", b"synthetic")},
        ))
        second = None
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.005)
            second = asyncio.create_task(client.post(
                f"/dossiers/{storage.id}/import", files={"file": ("second.dxf", b"synthetic")},
            ))
            with anyio.fail_after(2):
                while concurrency.analysis_limiter.statistics().tasks_waiting != 1:
                    await anyio.sleep(0.005)
            refused = await client.post("/profils/analyse", content=body())
            assert refused.status_code == 429
            assert refused.headers["retry-after"] == "5"
            assert refused.headers["x-content-type-options"] == "nosniff"
            assert body_reads == []
            assert (await client.get("/health")).status_code == 200
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert all(response.status_code == 303 for response in responses)
        assert concurrency.admission_limiter.borrowed_tokens == 0


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["analysis", "import"])
async def test_cancellation_during_upload_close_still_cleans_source(storage, monkeypatch, route):
    block_stage(monkeypatch, "none", threading.Event(), threading.Event())
    inspected = []
    original_inspect = main.inspect_dxf

    def inspect(path, filename):
        inspected.append(Path(path))
        return original_inspect(path, filename)

    async def cancelled_close():
        raise asyncio.CancelledError

    upload = UploadFile(filename="plan.dxf", file=BytesIO(b"synthetic"))
    monkeypatch.setattr(upload, "close", cancelled_close)
    monkeypatch.setattr(main, "inspect_dxf", inspect)
    try:
        with pytest.raises(asyncio.CancelledError):
            if route == "analysis":
                await main._create_analysis(upload)
            else:
                request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
                await main.import_plan(request, storage.id, upload)
    finally:
        upload.file.close()
    assert len(inspected) == 1
    assert not inspected[0].exists()
