# Run from the repository root. The trainer inherits the output of the locked CUDA build.
target "runtime" {
  context    = "training/gpu"
  dockerfile = "trainer.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "eve" {
  context    = "."
  dockerfile = "infrastructure/images/eve-trainer.Dockerfile"
  contexts   = { "eve-runtime.invalid/locked" = "target:runtime" }
  platforms  = ["linux/amd64"]
  tags       = ["opentour/eve-trainer:local"]
}
