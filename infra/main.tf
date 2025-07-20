provider "aws" {
    region = "eu-west-1"  
}

resource "aws_s3_bucket" "spotify_auth_cache" {
  bucket = "radek-spotify-auth-cache"
  force_destroy = true
}
