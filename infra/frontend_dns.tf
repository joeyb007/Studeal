# Frontend: studeal.site (apex + www) → Vercel. The api subdomain stays on
# the ALB (alb.tf); Vercel serves only the Next.js app.

resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "studeal.site"
  type    = "A"
  ttl     = 300
  records = ["76.76.21.21"]
}

resource "aws_route53_record" "www" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "www.studeal.site"
  type    = "CNAME"
  ttl     = 300
  records = ["cname.vercel-dns.com"]
}
