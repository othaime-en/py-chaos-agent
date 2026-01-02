
resource "aws_ecr_repository" "chaos_agent" {
  name = "py-chaos-agent"
}

resource "aws_ecr_repository" "target_app" {
  name = "target-app"
}

output "ecr_repo_urls" {
  value = {
    chaos_agent = aws_ecr_repository.chaos_agent.repository_url
    target_app  = aws_ecr_repository.target_app.repository_url
  }
}