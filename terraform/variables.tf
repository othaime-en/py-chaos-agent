variable "region" {
  default = "us-east-1"
}

variable "cluster_name" {
  default = "chaos-demo-eks"
}

variable "cluster_endpoint_private_access" {
  description = "Serve the Kubernetes API inside the VPC. Keep this true; nodes use it."
  type        = bool
  default     = true
}

variable "cluster_endpoint_public_access" {
  description = <<-EOT
    Serve the Kubernetes API on the internet too. Set to false for a private
    cluster: kubectl then works only from inside the VPC (VPN, bastion or SSM).
    Defaults to true so existing setups keep working.
  EOT
  type        = bool
  default     = true

  validation {
    condition     = var.cluster_endpoint_public_access || var.cluster_endpoint_private_access
    error_message = "At least one of the private and public cluster endpoints must be enabled."
  }
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "Source ranges allowed to reach the public endpoint. Narrow this to your own IPs."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.cluster_endpoint_public_access_cidrs) > 0 && alltrue([for c in var.cluster_endpoint_public_access_cidrs : can(cidrhost(c, 0))])
    error_message = "Provide at least one valid CIDR block, for example 203.0.113.7/32."
  }
}
