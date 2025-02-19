import json
import time
import requests
from typing import Tuple, List, Optional, Dict, Union
from datetime import datetime
import threading
import argparse
from transformers import AutoTokenizer, PreTrainedTokenizer, PreTrainedTokenizerFast

def get_tokenizer(
        pretrained_model_name_or_path: str, trust_remote_code: bool
) -> Union[PreTrainedTokenizer, PreTrainedTokenizerFast]:
    return AutoTokenizer.from_pretrained(
        pretrained_model_name_or_path,
        trust_remote_code=trust_remote_code
    )

class RateLimiter:
    def __init__(self, qps: float):
        self.interval = 1.0 / qps
        self.last_request_time = 0
        self.lock = threading.Lock()
    
    def wait(self):
        """Wait if necessary to maintain the desired QPS."""
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            if time_since_last < self.interval:
                sleep_time = self.interval - time_since_last
                time.sleep(sleep_time)
            self.last_request_time = time.time()

class PromptSelector:
    def __init__(self, trace_file: str, 
                 model_endpoint: str = "http://localhost:8888/v1/chat/completions",
                 qps: float = 2.0,
                 temperature: float = 0.0,
                 model_name: str = "Qwen/Qwen2.5-Coder-7B-Instruct"):
        self.trace_file = trace_file
        self.model_endpoint = model_endpoint
        self.tokenizer = get_tokenizer(
            pretrained_model_name_or_path=model_name, 
            trust_remote_code=True
        )
        self.rate_limiter = RateLimiter(qps)
        self.temperature = temperature
        
    def count_tokens(self, text: str) -> int:
        """Estimate token count using VLLM's tokenizer."""
        return len(self.tokenizer.encode(text))
    
    def get_completion_tokens(self, prompt: str, model: str = "deepseek-coder-33b-instruct") -> Tuple[Optional[int], Dict]:
        """Get actual completion tokens by querying the model with rate limiting."""
        self.rate_limiter.wait()
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer any_key"
        }
        
        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature
        }
        
        try:
            response = requests.post(self.model_endpoint, headers=headers, json=data)
            response.raise_for_status()
            response_data = response.json()
            completion_tokens = response_data.get("usage", {}).get("completion_tokens")
            return completion_tokens, response_data
        except Exception as e:
            print(f"Error querying model: {e}")
            return None, {}

    def find_matching_prompts(self, target_input_tokens: int, min_output_tokens: int, 
                            input_tolerance: float = 0.1, max_candidates: Optional[int] = None) -> List[Tuple[str, int, int, Dict]]:
        """
        Find prompts and save results to a file.
        Returns list of tuples (prompt, input_tokens, output_tokens, response_data)
        """
        matching_prompts = []
        best_input_diff = float('inf')
        candidates = []
        
        input_min = int(target_input_tokens * (1 - input_tolerance))
        input_max = int(target_input_tokens * (1 + input_tolerance))
        
        print(f"Scanning trace file for candidates...")
        print(f"Input token range: {input_min} - {input_max}")
        
        # First pass: collect all candidates based on input length
        with open(self.trace_file, 'r') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    messages = data.get("messages", [])
                    prompt = "\n".join(msg.get("content", "") for msg in messages)
                    input_tokens = self.count_tokens(prompt)
                    
                    if input_min <= input_tokens <= input_max:
                        input_diff = abs(input_tokens - target_input_tokens)
                        candidates.append((prompt, input_tokens, input_diff))
                
                except (json.JSONDecodeError, Exception) as e:
                    continue
        
        # Sort candidates by input difference
        candidates.sort(key=lambda x: x[2])
        
        # If max_candidates is not specified, use all candidates
        if max_candidates is not None:
            candidates = candidates[:max_candidates]
        
        print(f"\nFound {len(candidates)} candidates. Querying model for each...")
        print("-" * 80)
        
        current_input_diff = None
        found_valid_match = False
        
        for idx, (prompt, input_tokens, input_diff) in enumerate(candidates, 1):
            # If we've found a match and the input_diff is different, we can stop
            if found_valid_match and input_diff > current_input_diff:
                break
                
            print(f"\nCandidate {idx}/{len(candidates)}:")
            print(f"Input tokens: {input_tokens} (diff from target: {input_diff})")
            print(f"Prompt preview: {prompt[:200]}...")
            
            output_tokens, response_data = self.get_completion_tokens(prompt)
            
            if output_tokens and output_tokens >= min_output_tokens:
                if not found_valid_match or input_diff < current_input_diff:
                    matching_prompts = []
                    current_input_diff = input_diff
                    found_valid_match = True
                matching_prompts.append((prompt, input_tokens, output_tokens, response_data))
            
            print("-" * 80)
        
        self.save_results(matching_prompts, target_input_tokens, min_output_tokens)
        return matching_prompts

    def save_results(self, matching_prompts: List[Tuple[str, int, int, Dict]], 
                    target_input_tokens: int, min_output_tokens: int):
        """Save matching prompts to a JSON file."""
        # Only proceed if there are matching prompts to save
        if not matching_prompts:
            print("\nNo matching prompts found, skipping file creation.")
            return
            
        filename = f"result/prompts/prompt_in{target_input_tokens}_out{min_output_tokens}.json"
        
        # Create the benchmark-compatible format
        current_time = int(time.time() * 1000)  # Current timestamp in milliseconds
        benchmark_format = [{
            "Timestamp": current_time,
            "Requests": [{
                "Prompt": prompt,
                "Prompt Length": input_tokens,
                "Output Length": output_tokens,
                # Store additional metadata in a separate field
                "Metadata": {
                    "model_response": response_data,
                    "temperature": self.temperature
                }
            } for prompt, input_tokens, output_tokens, response_data in matching_prompts]
        }]
        
        # Write the formatted data
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(benchmark_format, f, indent=2)
        
        print(f"\nResults saved to: {filename}")

def parse_args():
    parser = argparse.ArgumentParser(description='Find prompts matching specific token criteria')
    parser.add_argument('--workload_dataset_file', type=str, required=True,
                      help='Path to the workload dataset file')
    parser.add_argument('--host', type=str, 
                      default='localhost',
                      help='Model endpoint host (default: localhost)')
    parser.add_argument('--port', type=int, 
                      default=8010,
                      help='Model endpoint port (default: 8010)')
    parser.add_argument('--input-tokens', type=int, required=True,
                      help='Target input token count')
    parser.add_argument('--min-output-tokens', type=int, required=True,
                      help='Minimum output token count')
    parser.add_argument('--tolerance', type=float, default=0.1,
                      help='Tolerance for input token matching (default: 0.1)')
    parser.add_argument('--qps', type=float, default=2.0,
                      help='Queries per second rate limit (default: 2.0)')
    parser.add_argument('--max-candidates', type=int, default=None,
                      help='Maximum number of candidates to query (default: None, use all candidates)')
    parser.add_argument('--temperature', type=float, default=0.0,
                      help='Temperature for model inference (default: 0.0)')
    return parser.parse_args()

def main():
    args = parse_args()
    start_time = time.time()
    
    print(f"\nStarting prompt search with parameters:")
    print(f"Target input tokens: {args.input_tokens}")
    print(f"Minimum output tokens: {args.min_output_tokens}")
    print(f"Tolerance: {args.tolerance}")
    print(f"QPS: {args.qps}")
    print(f"Max candidates: {args.max_candidates}")
    print(f"Model endpoint: http://{args.host}:{args.port}/v1/chat/completions")
    print("-" * 80)
    
    model_endpoint = f"http://{args.host}:{args.port}/v1/chat/completions"


    selector = PromptSelector(
        trace_file=args.workload_dataset_file,
        model_endpoint=model_endpoint,
        qps=args.qps,
        temperature=args.temperature
    )
    
    matching_prompts = selector.find_matching_prompts(
        target_input_tokens=args.input_tokens,
        min_output_tokens=args.min_output_tokens,
        input_tolerance=args.tolerance,
        max_candidates=args.max_candidates
    )
    
    print(f"\nFound {len(matching_prompts)} matching prompts:")
    for idx, (prompt, input_tokens, output_tokens, response_data) in enumerate(matching_prompts, 1):
        print(f"\nMatch {idx}:")
        print("=" * 80)
        print(f"Input tokens: {input_tokens}")
        print(f"Output tokens: {output_tokens}")
        print(f"Complete usage data: {response_data.get('usage', {})}")
        print("-" * 40)
        print("Prompt:")
        print(prompt)
        print("-" * 40)
        print("Model completion:")
        if 'choices' in response_data:
            print(response_data['choices'][0].get('message', {}).get('content', ''))
    
    end_time = time.time()
    print(f"\nTotal execution time: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    main()