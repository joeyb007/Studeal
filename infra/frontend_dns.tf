# Frontend: studeal.site (apex + www) → Vercel. The api subdomain stays on
# the ALB (alb.tf); Vercel serves only the Next.js app.

# Vercel's apex anycast IP. NOT 76.76.21.21 — that address was Vercel's
# documented apex target when this zone was first cut (and is still what
# `vercel domains add` printed on 2026-08-14), but it went dark by 08-16:
# globally connection-refused, taking the apex down while www (CNAME to
# cname.vercel-dns.com) kept serving. Verified 216.198.79.1 answers for
# studeal.site with a valid cert before switching.
resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "studeal.site"
  type    = "A"
  ttl     = 300
  records = ["216.198.79.1"]
}

resource "aws_route53_record" "www" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "www.studeal.site"
  type    = "CNAME"
  ttl     = 300
  records = ["cname.vercel-dns.com"]
}
