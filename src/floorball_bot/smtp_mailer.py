from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from floorball_bot.contact_requests import ContactDelivery, ContactMailer

_SUBJECTS = {
    "training": "Тренировки",
    "tournaments": "Турниры",
    "club": "Открытие клуба",
    "partnership": "Партнёрство",
    "other": "Другое",
}


def build_contact_email(delivery: ContactDelivery, *, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"floorball.kz <{sender}>"
    message["To"] = delivery.recipient
    message["Reply-To"] = delivery.reply_to
    message["Subject"] = f"[floorball.kz] {_SUBJECTS[delivery.subject]}"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = f"<contact-{delivery.request_id}@floorball.kz>"
    message["X-Floorball-Request-ID"] = str(delivery.request_id)
    message.set_content(
        "\n".join(
            [
                "Новая заявка с floorball.kz",
                "",
                f"Request ID: {delivery.request_id}",
                f"Язык: {delivery.locale}",
                f"Имя: {delivery.name}",
                f"Email для ответа: {delivery.reply_to}",
                f"Тема: {_SUBJECTS[delivery.subject]}",
                "",
                "Сообщение:",
                delivery.message,
            ]
        ),
        subtype="plain",
        charset="utf-8",
    )
    return message


class SmtpContactMailer(ContactMailer):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        timeout_seconds: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.timeout_seconds = timeout_seconds

    async def send(self, delivery: ContactDelivery) -> str:
        message = build_contact_email(delivery, sender=self.username)
        await asyncio.to_thread(self._send_sync, message)
        return str(message["Message-ID"])

    def _send_sync(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        client_context = (
            smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=context,
            )
            if self.use_ssl
            else smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds)
        )
        with client_context as client:
            if not self.use_ssl:
                client.starttls(context=context)
            client.login(self.username, self.password)
            client.send_message(message)
