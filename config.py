import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
WEB_USER   = os.environ["WEB_USER"]
WEB_PASS   = os.environ["WEB_PASS"]

LOGIN_URL  = "https://sumvirtual.unmsm.edu.pe/WebSum2/login"
TICKET_URL = "https://sumvirtual.unmsm.edu.pe/WebSum2/sum/comedor/EmitirTicket"

# 0-based indices inside the <select>; index 0 is always "Seleccione…"
SEDE_IDX     = int(os.getenv("SEDE_IDX", "1"))
SERVICIO_IDX = int(os.getenv("SERVICIO_IDX", "2"))

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
HEADLESS       = os.getenv("HEADLESS", "true").lower() != "false"
