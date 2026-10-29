# Tags are immutable: a pushed tag can never be pointed at different content, so
# what the cluster pulls is what was scanned and approved. Push a new version
# tag for every build (the manifests use 1.0.0, never :latest).
resource "aws_ecr_repository" "chaos_agent" {
  name                 = "py-chaos-agent"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "target_app" {
  name                 = "target-app"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keep the registry from growing forever. Untagged images go after a week; of
# the tagged ones, the newest 30 stay. (The catch-all rule must have the
# highest priority number.)
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = {
    chaos_agent = aws_ecr_repository.chaos_agent.name
    target_app  = aws_ecr_repository.target_app.name
  }

  repository = each.value
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest 30 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = { type = "expire" }
      }
    ]
  })
}

output "ecr_repo_urls" {
  value = {
    chaos_agent = aws_ecr_repository.chaos_agent.repository_url
    target_app  = aws_ecr_repository.target_app.repository_url
  }
}
