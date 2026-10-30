module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29"

  cluster_name    = var.cluster_name
  cluster_version = "1.31"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Who can reach the Kubernetes API. See variables.tf; set
  # cluster_endpoint_public_access = false for a private cluster.
  cluster_endpoint_private_access      = var.cluster_endpoint_private_access
  cluster_endpoint_public_access       = var.cluster_endpoint_public_access
  cluster_endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  enable_cluster_creator_admin_permissions = true

  eks_managed_node_groups = {
    chaos_demo = {
      min_size     = 1
      max_size     = 3
      desired_size = 2

      instance_types = ["t3.medium"]
      capacity_type  = "SPOT"
    }
  }

  tags = {
    Environment = "chaos-demo"
    Project     = "py-chaos-agent"
  }
}

# A warning, not an error: the demo is meant to be easy to start. It shows on
# every plan until the public endpoint is closed or narrowed.
check "cluster_endpoint_is_not_open_to_the_internet" {
  assert {
    condition     = !var.cluster_endpoint_public_access || !contains(var.cluster_endpoint_public_access_cidrs, "0.0.0.0/0")
    error_message = "The Kubernetes API endpoint is open to the whole internet. Set cluster_endpoint_public_access_cidrs to your own IP ranges, or cluster_endpoint_public_access = false."
  }
}
