# Resend email authentication. These records lived in Namecheap DNS from the
# original domain setup; the 2026-08-14 nameserver move to this zone dropped
# them (Resend's "Verified" badge is stale until re-check). Values from the
# Resend domain page for studeal.site, region us-east-1.

variable "resend_dkim_p" {
  description = "Full p=... value from Resend's resend._domainkey TXT record (public key — safe in code)"
  type        = string
  default     = "p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDH7iKdF5OX1KcmUq0aVoKatsXn+TaF5bGdR4FtFUN43UAth6EWdsaG76giBZVvo3S+CrqoctlY3muWoEkixbCR48Cd1pLOAfQZd+8iIoWkUE/sWIqgQ9xxh4Dgx/bEiauBG7Vm4bVGCviMdhZB4L2I+/Bqebv6Nmu0cp+w/HZz0wIDAQAB"
}

resource "aws_route53_record" "resend_dkim" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "resend._domainkey.studeal.site"
  type    = "TXT"
  ttl     = 300
  records = [var.resend_dkim_p]
}

resource "aws_route53_record" "resend_send_mx" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "send.studeal.site"
  type    = "MX"
  ttl     = 300
  records = ["10 feedback-smtp.us-east-1.amazonses.com"]
}

resource "aws_route53_record" "resend_send_spf" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "send.studeal.site"
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com ~all"]
}

# p=none: monitor-only DMARC — alignment reporting without rejecting mail
# while volume is tiny. Tighten to quarantine post-launch if reports are clean.
resource "aws_route53_record" "resend_dmarc" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "_dmarc.studeal.site"
  type    = "TXT"
  ttl     = 300
  records = ["v=DMARC1; p=none;"]
}
