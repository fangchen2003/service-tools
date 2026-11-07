"""Verify real app route registration without starting production lifecycles."""
import pytest


@pytest.mark.asyncio
async def test_real_application_mounts_public_verification():
    from app.main import app
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
        response = await client.get('/self-register/verify')
    assert response.status_code == 503  # Mounted but no lifespan-configured service.
    assert 'Discord 身份验证' in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_real_application_mounts_registration_controls(monkeypatch):
    from app.main import app
    from app import registration_admin
    import httpx
    def denied(request):
        from fastapi import HTTPException
        raise HTTPException(401, 'registration-auth-probe')
    monkeypatch.setattr(registration_admin, 'require_admin', denied)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
        response = await client.get('/admin/api/registration')
    assert response.status_code == 401
    assert response.json() == {'detail': 'registration-auth-probe'}
