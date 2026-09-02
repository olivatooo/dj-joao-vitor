# No EXPOSE: this bot never listens. Both the gateway and the voice WebSocket
# are outbound, and ICE works because the SFU is ice-lite -- it only ever
# replies to the source address of our STUN/RTP, so the UDP flow is
# outbound-initiated too. What it needs is egress, not ingress.
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py sbvoice.py ./

ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp
USER nobody
CMD ["python", "bot.py"]
