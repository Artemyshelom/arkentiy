"""
Email-клиент на базе Resend (https://resend.com).
100 писем/день бесплатно, простой API.

Если RESEND_API_KEY не задан — письма логируются, не отправляются.
"""

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, html: str) -> bool:
    """Отправляет email через Resend API. Возвращает True при успехе."""
    settings = get_settings()

    if not settings.resend_api_key:
        logger.info(f"EMAIL SKIP (no RESEND_API_KEY): to={to} subject={subject!r}")
        return False

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.email_from,
                    "to": to,
                    "subject": subject,
                    "html": html,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"Email sent id={data.get('id')} to={to}")
                return True
            else:
                logger.error(f"Resend error {resp.status_code}: {resp.text[:200]}")
                return False
    except Exception as e:
        logger.error(f"Email send exception to={to}: {e}")
        return False


async def send_verification_email(email: str, token: str) -> bool:
    """Письмо подтверждения email при регистрации."""
    settings = get_settings()
    url = f"{settings.base_url}/verify.html?token={token}"
    html = f"""<!DOCTYPE html>
<html lang="ru">
<body style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1a1a1a">
  <h2 style="color:#2563eb;margin-bottom:8px">Подтвердите email</h2>
  <p>Здравствуйте!</p>
  <p>Для завершения регистрации в <b>Аркентий</b> подтвердите ваш email-адрес:</p>
  <p style="margin:24px 0">
    <a href="{url}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold">
      Подтвердить email
    </a>
  </p>
  <p style="color:#6b7280;font-size:14px">Ссылка действительна <b>24 часа</b>.</p>
  <p style="color:#6b7280;font-size:14px">Если вы не регистрировались — проигнорируйте это письмо.</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
  <p style="color:#9ca3af;font-size:12px">Аркентий — автоматизация для Ёбидоёби</p>
</body>
</html>"""
    return await send_email(email, "Подтвердите email для Аркентий", html)


async def send_reset_email(email: str, token: str) -> bool:
    """Письмо сброса пароля."""
    settings = get_settings()
    url = f"{settings.base_url}/reset-password?token={token}"
    html = f"""<!DOCTYPE html>
<html lang="ru">
<body style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1a1a1a">
  <h2 style="color:#2563eb;margin-bottom:8px">Сброс пароля</h2>
  <p>Здравствуйте!</p>
  <p>Вы запросили сброс пароля в <b>Аркентий</b>:</p>
  <p style="margin:24px 0">
    <a href="{url}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold">
      Сбросить пароль
    </a>
  </p>
  <p style="color:#6b7280;font-size:14px">Ссылка действительна <b>1 час</b>.</p>
  <p style="color:#6b7280;font-size:14px">Если вы не запрашивали сброс — проигнорируйте это письмо.</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
  <p style="color:#9ca3af;font-size:12px">Аркентий — автоматизация для Ёбидоёби</p>
</body>
</html>"""
    return await send_email(email, "Сброс пароля Аркентий", html)
