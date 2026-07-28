"""
Client for communicating with Whishper API for audio transcription.
"""
import os
import requests
import time


class WhishperClient:
    def __init__(self, base_url=None):
        # Use environment variable if set, otherwise try defaults
        # This allows:
        # - Docker containers to use host.docker.internal
        # - Native Windows/Mac/Linux to use localhost
        # - Custom deployments to override via env var
        if base_url is None:
            base_url = os.getenv(
                'WHISHPER_API_URL',
                'http://localhost:8082/api/transcriptions'
            )
        self.base_url = base_url
        print(f"DEBUG: WhishperClient initialized with URL: {self.base_url}")
    
    def test_endpoints(self):
        """Test different possible endpoints to find the right one"""
        test_endpoints = [
            "http://localhost:8082/api/transcriptions",
            "http://localhost:8082/api/transcriptions/",
            "http://localhost:8082/api/transcribe/",
            "http://localhost:8082/api/transcribe",
        ]
        
        for endpoint in test_endpoints:
            try:
                print(f"Testing endpoint: {endpoint}")
                response = requests.get(endpoint, timeout=5)
                print(f"  Status: {response.status_code}")
                if response.status_code != 404:
                    print(f"  Content: {response.text[:200]}")
            except requests.exceptions.RequestException as e:
                print(f"  Error: {e}")
            print()
    
    def transcribe_audio(self, audio_path, model_size="small", device="cpu", language="auto", timeout_s=600):
        """
        Sends an audio file to the Whishper API for transcription.
        Updated to match Whishper's expected format and handle async processing.
        """
        file_size = os.path.getsize(audio_path)
        print(f"Uploading file: {audio_path} ({file_size / (1024*1024):.2f} MB)")
        
        try:
            with open(audio_path, 'rb') as f:
                files = {'file': (os.path.basename(audio_path), f, 'audio/wav')}
                
                # Try with exact parameter names that might be expected
                data = {
                    'modelSize': model_size,    # Try camelCase
                    'device': device,
                    'language': language,
                    'task': 'transcribe',       # Explicit task
                    'sourceUrl': '',            # Empty source URL (for file uploads)
                    'fileName': os.path.basename(audio_path)  # Explicit filename
                }
                
                print(f"Making request to: {self.base_url}")
                print(f"Data: {data}")
                
                response = requests.post(
                    self.base_url, 
                    files=files,
                    data=data,
                    timeout=timeout_s
                )
                
                print(f"Response status: {response.status_code}")
                
                if response.status_code != 200:
                    print(f"Response content: {response.text}")
                    response.raise_for_status()
                
                # Parse the response to get the job ID
                job_data = response.json()
                job_id = job_data.get('id')
                
                if not job_id:
                    print(f"No job ID in response: {job_data}")
                    return None
                    
                print(f"Job submitted: {job_id}")
                
                # Poll for completion
                return self._poll_transcription_status(job_id, timeout_s)
                
        except requests.exceptions.RequestException as e:
            print(f"Error during API request: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response content: {e.response.text}")
            return None
    
    def _poll_transcription_status(self, job_id, timeout_s=600, poll_interval=5):
        """
        Polls the transcription status until completion or timeout.
        """
        start_time = time.time()
        print("Waiting for transcription...")
        
        while time.time() - start_time < timeout_s:
            try:
                status_url = f"{self.base_url.rstrip('/')}/{job_id}"
                response = requests.get(status_url, timeout=30)
                
                if response.status_code != 200:
                    print(f"Status check failed: {response.status_code}")
                    time.sleep(poll_interval)
                    continue
                    
                job_data = response.json()
                status = job_data.get('status')
                
                if status == 2:  # Success
                    print("Transcription completed successfully!")
                    return job_data.get('result', {})
                elif status == -1:  # Error
                    print(f"Transcription failed: {job_data}")
                    return None
                elif status in [0, 1]:  # Pending/Processing
                    print(f"Status: {status} - Still processing...")
                    time.sleep(poll_interval)
                    continue
                else:
                    print(f"Unknown status: {status}")
                    time.sleep(poll_interval)
                    continue
                    
            except requests.exceptions.RequestException as e:
                print(f"Error checking status: {e}")
                time.sleep(poll_interval)
                continue
        
        print("Timeout waiting for transcription to complete")
        return None


def transcribe_audio(audio_path, model_size="small", device="cpu", language="auto", timeout_s=600):
    """
    Transcribe audio. Uses the embedded faster-whisper transcriber by default
    (no Docker required). Set USE_WHISHPER=1 (and optionally WHISHPER_API_URL)
    to use an external Whishper server instead.
    """
    if os.getenv('USE_WHISHPER', '').lower() in ('1', 'true', 'yes'):
        client = WhishperClient()
        return client.transcribe_audio(audio_path, model_size, device, language, timeout_s)

    import local_transcriber
    return local_transcriber.transcribe_audio(audio_path, model_size, device, language, timeout_s)