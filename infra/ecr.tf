# The image registry: docker push sends the built image here; ECS task
# definitions pull from here by tag.

resource "aws_ecr_repository" "app" {
  name = "studeal"

  image_scanning_configuration {
    scan_on_push = true    # free CVE scan on every push
  }
}

# Old images accumulate ~1GB each; keep the last 10 and let AWS delete the
# rest. Rollbacks rarely reach past the previous few deploys.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
