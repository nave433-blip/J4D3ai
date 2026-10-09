class Jade < Formula
  desc "Jade: local AI assistant for macOS and Linux"
  homepage "https://github.com/nave433-blip/jarvis-dev"
  # Release checklist (see PACKAGING.md): on a new release, point this at the
  # new tag tarball and update sha256 to match (sha256sum of the download).
  url "https://github.com/nave433-blip/jarvis-dev/archive/refs/tags/v0.1.7.tar.gz"
  sha256 "bd6c443f0a93dba7250c38a1d6b0d0207e4143e73d4dfa02db60e592f1c23049"
  license "MIT"

  depends_on "python@3.12"
  depends_on "portaudio"
  depends_on "ollama"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/cortana", "--help"
  end
end
