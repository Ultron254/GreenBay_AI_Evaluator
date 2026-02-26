#!/usr/bin/env python3
"""
Simple Image Analysis Script using LLM with Vision

This script uses OpenAI's GPT-4 Vision model to analyze images and provide
detailed analysis output. It can process single images or batches of images.

Usage:
    python image_analyzer.py <image_path>
    python image_analyzer.py <image_path1> <image_path2> ...
    python image_analyzer.py --batch <directory_path>
    python image_analyzer.py "https://example.com/image.jpg"
"""

import os
import sys
import base64
import argparse
from pathlib import Path
from typing import List, Optional
import json
import tempfile
import urllib.parse

from openai import OpenAI
from PIL import Image
import requests

# Import project configuration
from app.config import get_settings


class ImageAnalyzer:
    """Image analyzer using OpenAI's GPT-4 Vision model."""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o"):
        """Initialize the image analyzer.
        
        Args:
            api_key: OpenAI API key. If None, will use from environment/config.
            model: OpenAI model to use for vision analysis.
        """
        self.settings = get_settings()
        self.api_key = api_key or self.settings.openai_api_key
        self.model = model
        
        if self.api_key == "test_key":
            print("Warning: Using test API key. Please set OPENAI_API_KEY environment variable.")
        
        self.client = OpenAI(api_key=self.api_key)
    
    def encode_image(self, image_path: str) -> str:
        """Encode image to base64 string.
        
        Args:
            image_path: Path to the image file.
            
        Returns:
            Base64 encoded image string.
        """
        try:
            with open(image_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            raise ValueError(f"Error encoding image {image_path}: {e}")
    
    def is_url(self, path: str) -> bool:
        """Check if the path is a URL.
        
        Args:
            path: Path or URL to check.
            
        Returns:
            True if it's a URL, False otherwise.
        """
        return path.startswith(('http://', 'https://'))
    
    def download_image_from_url(self, url: str) -> str:
        """Download image from URL to a temporary file.
        
        Args:
            url: URL of the image to download.
            
        Returns:
            Path to the downloaded temporary file.
        """
        try:
            # Parse URL to get filename
            parsed_url = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename or '.' not in filename:
                filename = 'downloaded_image.jpg'
            
            # Create temporary file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'_{filename}')
            temp_path = temp_file.name
            temp_file.close()
            
            # Download the image
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            # Save to temporary file
            with open(temp_path, 'wb') as f:
                f.write(response.content)
            
            return temp_path
            
        except Exception as e:
            raise ValueError(f"Error downloading image from URL {url}: {e}")
    
    def validate_image(self, image_path: str) -> bool:
        """Validate that the file is a valid image.
        
        Args:
            image_path: Path to the image file.
            
        Returns:
            True if valid image, False otherwise.
        """
        try:
            with Image.open(image_path) as img:
                img.verify()
            return True
        except Exception:
            return False
    
    def analyze_image(self, image_path: str, analysis_prompt: str = None) -> dict:
        """Analyze a single image using GPT-4 Vision.
        
        Args:
            image_path: Path to the image file or URL.
            analysis_prompt: Custom prompt for analysis. If None, uses default.
            
        Returns:
            Dictionary containing analysis results.
        """
        temp_file_path = None
        actual_image_path = image_path
        
        try:
            # Handle URLs
            if self.is_url(image_path):
                print(f"Downloading image from URL: {image_path}")
                temp_file_path = self.download_image_from_url(image_path)
                actual_image_path = temp_file_path
                print(f"Downloaded to temporary file: {temp_file_path}")
            
            # Check if file exists (for local files)
            if not self.is_url(image_path) and not os.path.exists(actual_image_path):
                raise FileNotFoundError(f"Image file not found: {image_path}")
            
            if not self.validate_image(actual_image_path):
                raise ValueError(f"Invalid image file: {image_path}")
            
            # Default analysis prompt
            if analysis_prompt is None:
                analysis_prompt = """
Please analyze this image in detail and provide:
1. A general description of what you see
2. Key objects, people, or elements present
3. Colors, composition, and visual style
4. Any text visible in the image
5. Context or setting (if identifiable)
6. Any notable features or interesting details
7. Overall mood or atmosphere

Please be thorough and descriptive in your analysis.
"""
        
            # Encode the image
            base64_image = self.encode_image(actual_image_path)
            
            # Make API call
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": analysis_prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=1000,
                temperature=0.7
            )
            
            analysis_text = response.choices[0].message.content
            
            return {
                "image_path": image_path,
                "analysis": analysis_text,
                "model_used": self.model,
                "tokens_used": response.usage.total_tokens if response.usage else None,
                "success": True
            }
            
        except Exception as e:
            return {
                "image_path": image_path,
                "analysis": None,
                "error": str(e),
                "success": False
            }
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    print(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as cleanup_error:
                    print(f"Warning: Could not clean up temporary file {temp_file_path}: {cleanup_error}")

    def analyze_multiple_images_single_call(self, image_paths: List[str], analysis_prompt: str = None) -> dict:
        """Analyze multiple images in a single LLM call for comparative analysis.
        
        Args:
            image_paths: List of image file paths or URLs.
            analysis_prompt: Custom prompt for analysis. If None, uses default.
            
        Returns:
            Dictionary containing analysis results.
        """
        temp_files = []
        actual_image_paths = []
        
        try:
            # Process all images (download URLs, validate)
            for image_path in image_paths:
                temp_file_path = None
                actual_image_path = image_path
                
                if self.is_url(image_path):
                    print(f"Downloading image from URL: {image_path}")
                    temp_file_path = self.download_image_from_url(image_path)
                    actual_image_path = temp_file_path
                    temp_files.append(temp_file_path)
                
                if not self.is_url(image_path) and not os.path.exists(actual_image_path):
                    raise FileNotFoundError(f"Image file not found: {image_path}")
                
                if not self.validate_image(actual_image_path):
                    raise ValueError(f"Invalid image file: {image_path}")
                
                actual_image_paths.append(actual_image_path)
            
            # Default analysis prompt for multiple images
            if analysis_prompt is None:
                analysis_prompt = f"""
    Please analyze these {len(image_paths)} images and provide:
    1. A general description of what you see in each image
    2. Key objects, people, or elements present in each
    3. Colors, composition, and visual style for each image
    4. Any text visible in the images
    5. Context or setting (if identifiable) for each
    6. Notable features or interesting details in each
    7. Overall mood or atmosphere for each image
    8. **Comparative analysis**: How do these images relate to each other? What similarities and differences do you notice?

    Please be thorough and descriptive in your analysis, and clearly indicate which image you're referring to in each section.
    """
            
            # Prepare content array with text prompt and all images
            content = [{"type": "text", "text": analysis_prompt}]
            
            for i, image_path in enumerate(actual_image_paths, 1):
                base64_image = self.encode_image(image_path)
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                })
            
            # Make API call with multiple images
            # Use response_format to enforce JSON if prompt asks for it
            response_kwargs = {
                "model": self.model,
                "messages": [{
                    "role": "user",
                    "content": content
                }],
                "max_tokens": 2000,  # Increased for multiple images
                "temperature": 0.7
            }
            
            # Check if prompt asks for JSON format
            if "JSON format" in analysis_prompt or "Return ONLY valid JSON" in analysis_prompt:
                response_kwargs["response_format"] = {"type": "json_object"}
            
            response = self.client.chat.completions.create(**response_kwargs)
            
            analysis_text = response.choices[0].message.content
            
            return {
                "image_paths": image_paths,
                "analysis": analysis_text,
                "model_used": self.model,
                "tokens_used": response.usage.total_tokens if response.usage else None,
                "success": True,
                "num_images": len(image_paths)
            }
            
        except Exception as e:
            return {
                "image_paths": image_paths,
                "analysis": None,
                "error": str(e),
                "success": False
            }
        finally:
            # Clean up temporary files
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                        print(f"Cleaned up temporary file: {temp_file}")
                    except Exception as cleanup_error:
                        print(f"Warning: Could not clean up temporary file {temp_file}: {cleanup_error}")
        
    def analyze_images_batch(self, image_paths: List[str], analysis_prompt: str = None) -> List[dict]:
        """Analyze multiple images in batch.
        
        Args:
            image_paths: List of image file paths.
            analysis_prompt: Custom prompt for analysis. If None, uses default.
            
        Returns:
            List of analysis results for each image.
        """
        results = []
        
        for image_path in image_paths:
            print(f"Analyzing: {image_path}")
            result = self.analyze_image(image_path, analysis_prompt)
            results.append(result)
            
            if result["success"]:
                print(f"✓ Successfully analyzed {image_path}")
            else:
                print(f"✗ Failed to analyze {image_path}: {result.get('error', 'Unknown error')}")
        
        return results
    
    def analyze_directory(self, directory_path: str, analysis_prompt: str = None) -> List[dict]:
        """Analyze all images in a directory.
        
        Args:
            directory_path: Path to directory containing images.
            analysis_prompt: Custom prompt for analysis. If None, uses default.
            
        Returns:
            List of analysis results for each image.
        """
        if not os.path.isdir(directory_path):
            raise ValueError(f"Directory not found: {directory_path}")
        
        # Supported image extensions
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}
        
        # Find all image files
        image_paths = []
        for file_path in Path(directory_path).rglob('*'):
            if file_path.suffix.lower() in image_extensions:
                image_paths.append(str(file_path))
        
        if not image_paths:
            print(f"No image files found in directory: {directory_path}")
            return []
        
        print(f"Found {len(image_paths)} image(s) to analyze")
        return self.analyze_images_batch(image_paths, analysis_prompt)
    
    def save_results(self, results: List[dict], output_file: str = None):
        """Save analysis results to a JSON file.
        
        Args:
            results: List of analysis results.
            output_file: Output file path. If None, generates automatic filename.
        """
        if output_file is None:
            output_file = f"image_analysis_results_{len(results)}_images.json"
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"Results saved to: {output_file}")
        except Exception as e:
            print(f"Error saving results: {e}")


def main():
    """Main function to run the image analyzer."""
    parser = argparse.ArgumentParser(description="Analyze images using LLM with vision")
    parser.add_argument("images", nargs="*", help="Image file paths to analyze")
    parser.add_argument("--batch", help="Directory path to analyze all images in batch")
    parser.add_argument("--prompt", help="Custom analysis prompt")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model to use (default: gpt-4o)")
    parser.add_argument("--multi-single-call", action="store_true", 
                   help="Analyze multiple images in a single LLM call for comparative analysis")
    
    args = parser.parse_args()
    
    # Initialize analyzer
    try:
        analyzer = ImageAnalyzer(model=args.model)
    except Exception as e:
        print(f"Error initializing analyzer: {e}")
        sys.exit(1)
    
    results = []
    
    try:
        if args.multi_single_call and len(args.images) > 1:
            result = analyzer.analyze_multiple_images_single_call(args.images, args.prompt)
            results = [result]  # Wrap in list for consistent handling
        elif args.batch:
            results = analyzer.analyze_directory(args.batch, args.prompt)
        elif args.images:
            results = analyzer.analyze_images_batch(args.images, args.prompt)
        else:
            print("Please provide image paths or use --batch for directory analysis")
            parser.print_help()
            sys.exit(1)
        
        # Print results
        print("\n" + "="*60)
        print("ANALYSIS RESULTS")
        print("="*60)
        
        for i, result in enumerate(results, 1):
            # Handle both single image and multi-image results
            if "image_paths" in result:
                print(f"\n[{i}] Images: {len(result['image_paths'])} images analyzed together")
                print("-" * 40)
            else:
                print(f"\n[{i}] Image: {result['image_path']}")
                print("-" * 40)
            
            if result["success"]:
                print(result["analysis"])
                if result.get("tokens_used"):
                    print(f"\nTokens used: {result['tokens_used']}")
                if result.get("num_images"):
                    print(f"Number of images: {result['num_images']}")
            else:
                print(f"Error: {result.get('error', 'Unknown error')}")
        
        # Save results if requested
        if args.output or len(results) > 1:
            analyzer.save_results(results, args.output)
        
    except Exception as e:
        print(f"Error during analysis: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
