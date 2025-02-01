import argparse
import logging
import time
import asyncio
import openai
import json
import os
import httpx
import matplotlib.pyplot as plt
import random
import psutil
from datetime import datetime
import csv
from utils import (load_workload, wrap_prompt_as_chat_message)

logging.basicConfig(level=logging.INFO)

async def send_request_with_httpx(args, client, prompt, output_file, completion_map, batch_id=-1, request_id=-1):
   start_time = asyncio.get_event_loop().time()
   if not args.endpoint.startswith(('http://', 'https://')):
       args.endpoint = 'http://' + args.endpoint
   try:
        response = await client.post(
            f"{args.endpoint}/v1/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {args.api_key}", 
                "routing-strategy": args.routing_strategy
            },
            json={
                "model": args.model,
                "prompt": prompt,
                "temperature": 0,
                "max_tokens": 2048
            },
        )
        try:
            data = response.json()
            end_time = asyncio.get_event_loop().time()
            latency = end_time - start_time
            result = {
                "status_code": response.status_code,
                "start_time": start_time,
                "end_time": end_time,
                "latency": latency,
                "throughput": data['usage']['completion_tokens'] / latency,
                "prompt_tokens": data['usage']['prompt_tokens'],
                "output_tokens": data['usage']['completion_tokens'],
                "total_tokens": data['usage']['total_tokens'],
                "input": prompt,
                "output": data['choices'][0]['text'],
            }
            if completion_map[request_id] != 0:
                logging.error(f"Request {request_id} already completed")
                assert False
            completion_map[request_id] = 1
            logging.warning(f"Batch {batch_id}, Request {request_id}, completed in {latency:.2f} seconds with throughput {result['throughput']:.2f} tokens/s")
            logging.warning(f"Total sent requests so far: {len(completion_map)}, completed requests: {sum(completion_map.values())}, completion_ratio: {sum(completion_map.values()) / len(completion_map)*100:.2f}%")
        except Exception as e:
            logging.error(f"Status: {response.status_code}, Raw response: {response.text}")
            logging.error(f"Error parsing response from {args.endpoint}: {str(e)}")
            result = {
                "status_code": response.status_code,
                "start_time": start_time,
                "end_time": None,
                "latency": None,
                "throughput": None,
                "prompt_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "input": prompt,
                "output": None,
            }
        
        ## Write result to JSONL file
        output_file.write(json.dumps(result) + "\n")
        # output_file.flush() # this is overhead on cpu and not necessary

        return result
   except Exception as e:
       logging.error(f"Error, send_request_with_httpx, {repr(e)}")
       return None

# Asynchronous request handler
async def send_request(api_key, client, model, endpoint, prompt, output_file, completion_map, batch_id=-1, request_id=-1):
    start_time = asyncio.get_event_loop().time()
    data = {
        "model": model,
        "prompt": prompt,
        "temperature": 0,
        "max_tokens": 2048
    }

    logging.warning("-"*40)
    logging.warning(f"curl -X POST {endpoint}/v1/completions \\"
            f"-H 'Content-Type: application/json' \\"
            f"-H 'Authorization: Bearer {api_key}' \\"
            f"-H 'routing-strategy: least-request' \\"
            f"-d '{json.dumps(data)}'")
    logging.warning("-"*40)
    try:
        response = await client.completions.create(
            model=model,
            prompt=prompt,
            temperature=0,
            max_tokens=2048
        )
        end_time = asyncio.get_event_loop().time()
        latency = end_time - start_time
        prompt_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        total_tokens = response.usage.total_tokens
        throughput = output_tokens / latency
        output_text = response.choices[0].message.content

        result = {
            "input": prompt,
            "output": output_text,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "start_time": start_time,
            "end_time": end_time,
            "latency": latency,
            "throughput": throughput
        }

        # Write result to JSONL file
        output_file.write(json.dumps(result) + "\n")
        output_file.flush()  # Ensure data is written immediately to the file
        logging.warning(f"Batch {batch_id}, Request {request_id}, completed in {latency:.2f} seconds with throughput {throughput:.2f} tokens/s, request {prompt} response {response}")
        if completion_map[request_id] != 0:
            logging.error(f"Request {request_id} already completed")
            assert False
        completion_map[request_id] = 1
        logging.info(f"Total sent requests so far: {len(completion_map)}, completed requests: {sum(completion_map.values())}, completion_ratio: {sum(completion_map.values()) / len(completion_map)*100:.2f}%")
        return result
    except Exception as e:
        logging.error(f"Error sending request to at {endpoint}: {str(e)}")
        return None

def collapse_workload(load_struct, workload_path, minimum_time_unit, write):
    # Collapse requests
    collapsed_requests = {}
    for requests_dict in load_struct:
        original_ts = int(requests_dict["timestamp"])
        collapsed_ts = (original_ts // minimum_time_unit) * minimum_time_unit
        if collapsed_ts not in collapsed_requests:
            collapsed_requests[collapsed_ts] = []
        collapsed_requests[collapsed_ts].extend([{
            "Prompt Length": request.get("Prompt Length"),
            "Output Length": request.get("Output Length"),
            "prompt": request["prompt"]
        } for request in requests_dict["requests"]])
    # Write collapsed workload
    if write:
        collapsed_workload_path = workload_path.rsplit('.', 1)[0] + '_collapsed.jsonl'
        with open(collapsed_workload_path, 'w', encoding='utf-8') as f:
            for ts in sorted(collapsed_requests.keys()):
                entry = {
                    "timestamp": ts,
                    "requests": collapsed_requests[ts]
                }
                f.write(json.dumps(entry) + '\n')
        logging.info(f"Written collapsed workload to {collapsed_workload_path}")
    return collapsed_requests

def scale_workload(workload, target_avg_rps):
    min_ts = min(entry['timestamp'] for entry in workload)
    second_counts = {}
    for entry in workload:
        second = (entry['timestamp'] - min_ts) // 1000
        if second not in second_counts:
            second_counts[second] = []
        second_counts[second].extend(entry['requests'])
    total_requests = sum(len(reqs) for reqs in second_counts.values())
    total_seconds = max(second_counts.keys()) + 1
    current_avg_rps = total_requests / total_seconds
    scale_factor = target_avg_rps / current_avg_rps
    scaled_workload = []
    remaining_fraction = 0.0
    for second, requests in sorted(second_counts.items()):
        exact_requests = len(requests) * scale_factor + remaining_fraction
        num_requests = int(exact_requests)
        remaining_fraction = exact_requests - num_requests
        if num_requests > 0:
            sampled_requests = random.sample(requests, min(num_requests, len(requests)))
            scaled_workload.append({
                'timestamp': second * 1000 + min_ts,
                'requests': sampled_requests
            })
    return scaled_workload



class ResourceMonitor:
    def __init__(self, output_dir):
        self.process = psutil.Process(os.getpid())
        self.start_time = time.time()
        self.output_dir = output_dir
        self.metrics_file = f"{output_dir}/resource_metrics.csv"
        self.setup_metrics_file()
        
    def setup_metrics_file(self):
        with open(self.metrics_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'elapsed_time', 
                'memory_mb', 'cpu_percent',
                'active_connections', 'open_files',
                'network_bytes_sent', 'network_bytes_recv',
                'io_read_mb', 'io_write_mb'
            ])
    
    async def monitor_resources(self, interval=1.0):
        """Monitor system resources every interval seconds"""
        net_io_start = psutil.net_io_counters()
        disk_io_start = psutil.disk_io_counters()
        
        while True:
            try:
                current_time = time.time()
                elapsed = current_time - self.start_time
                
                # Memory usage in MB
                memory_mb = self.process.memory_info().rss / 1024 / 1024
                
                # CPU usage
                cpu_percent = self.process.cpu_percent()
                
                # Network connections
                connections = len(self.process.connections())
                
                # Open files
                open_files = len(self.process.open_files())
                
                # Network I/O
                net_io_now = psutil.net_io_counters()
                net_bytes_sent = net_io_now.bytes_sent - net_io_start.bytes_sent
                net_bytes_recv = net_io_now.bytes_recv - net_io_start.bytes_recv
                
                # Disk I/O
                disk_io_now = psutil.disk_io_counters()
                io_read_mb = (disk_io_now.read_bytes - disk_io_start.read_bytes) / 1024 / 1024
                io_write_mb = (disk_io_now.write_bytes - disk_io_start.write_bytes) / 1024 / 1024
                
                # Write metrics to file
                with open(self.metrics_file, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        datetime.now().isoformat(),
                        f"{elapsed:.2f}",
                        f"{memory_mb:.2f}",
                        f"{cpu_percent:.1f}",
                        connections,
                        open_files,
                        net_bytes_sent,
                        net_bytes_recv,
                        f"{io_read_mb:.2f}",
                        f"{io_write_mb:.2f}"
                    ])
                
                # Log current status
                logging.info(
                    f"[METRICS] Elapsed: {elapsed:.1f}s, Memory: {memory_mb:.1f}MB, "
                    f"CPU: {cpu_percent}%, Connections: {connections}, "
                    f"Network: ↑{net_bytes_sent/1024/1024:.1f}MB ↓{net_bytes_recv/1024/1024:.1f}MB"
                )
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                logging.error(f"Error in resource monitoring: {str(e)}")
                await asyncio.sleep(interval)


async def new_benchmark_prescheduling2(args):
    # # openai client
    # client = openai.AsyncOpenAI(
    #     api_key=api_key,
    #     base_url=args.endpoint + "/v1",
    #     default_headers={"routing-strategy": "least-request"},
    # )

    ## resource monitor
    # monitor = ResourceMonitor(args.output_dir)
    # monitor_task = asyncio.create_task(monitor.monitor_resources())

    ## httpx client
    client = httpx.AsyncClient(
        timeout=300.0,
        limits=httpx.Limits(
            max_connections=4096,        # ulimit -n 65536
            max_keepalive_connections=1024 # About 20% of max_connections is a good ratio
        )
    )

    load_struct = load_workload(args.workload_path)
    # load_struct = scale_workload(load_struct, target_avg_rps=5)
    collapsed_wrk = collapse_workload(load_struct, args.workload_path, 1, False)
    rps_dict = {}
    for ts, requests in collapsed_wrk.items():
        second = ts//1000
        if second not in rps_dict:
            rps_dict[second] = 0
        rps_dict[second] += len(requests)
    rps_list = sorted(rps_dict.items())
    with open(f"{args.output_dir}/intended_rps.csv", 'w', encoding='utf-8') as f:
        for ts, rps in rps_list:
            f.write(f"{ts},{rps}\n")
    with open(f"{args.output_dir}/intended_traffic.csv", 'w', encoding='utf-8') as f:
        for ts, requests in collapsed_wrk.items():
            f.write(f"{ts/1000},{len(requests)}\n")
    logging.info(f"expected load: {rps_list}")
    base_time = time.time()
    num_requests = 0
    all_tasks = []
    num_requests_sent = 0
    request_id = 0
    completion_map = {}
    with open(args.output_file_path, 'w', encoding='utf-8') as f_out:
        sorted_timestamps = sorted(collapsed_wrk.keys())
        num_requests = sum(len(collapsed_wrk[ts]) for ts in sorted_timestamps)
        logging.info(f"Starting benchmark with {len(sorted_timestamps)} batches, total {num_requests} requests")
        try:
            for batch_num, ts in enumerate(sorted_timestamps):

                ## resource monitoring
                # batch_start_time = time.time()
                # batch_start_mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024

                formatted_prompts = [request["prompt"] for request in collapsed_wrk[ts]]
                target_time = base_time + (ts / 1000.0)
                current_time = time.time()
                sleep_duration = target_time - current_time
                if sleep_duration > 0:
                    logging.info(f"Waiting {sleep_duration:.2f}s before sending batch {batch_num} with {len(formatted_prompts)} requests")
                    await asyncio.sleep(sleep_duration)
                    logging.info(f"Sending batch {batch_num} with {len(formatted_prompts)} requests at {(time.time()-base_time):.2f}s (on schedule)")
                else:
                    logging.info(f"Sending batch {batch_num} with {len(formatted_prompts)} requests at {(time.time()-base_time):.2f}s (behind by {-sleep_duration:.2f}s)")
                # Create tasks for each request in the batch but don't await them
                batch_tasks = []
                for prompt in formatted_prompts:
                    completion_map[request_id] = 0
                    # batch_tasks.append(asyncio.create_task(send_request(api_key, client, model, endpoint, wrap_prompt_as_chat_message(prompt), f_out, completion_map, batch_num, request_id)))
                    batch_tasks.append(asyncio.create_task(send_request_with_httpx(args, client, wrap_prompt_as_chat_message(prompt), f_out, completion_map, batch_num, request_id)))
                    request_id += 1
                all_tasks.extend(batch_tasks)
                num_requests_sent += len(formatted_prompts)
                
                ## resource monitoring
                # batch_end_time = time.time()
                # batch_end_mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                # logging.info(
                #     f"Batch {batch_num} metrics - Duration: {batch_end_time - batch_start_time:.2f}s, "
                #     f"Memory change: {batch_end_mem - batch_start_mem:.1f}MB"
                # )

            # Wait for all requests to complete after all batches have been sent
            await asyncio.gather(*all_tasks)
            total_time = time.time() - base_time
            actual_qps = num_requests / total_time
            logging.info(f"Completed {num_requests} requests in {total_time:.2f}s (actual QPS: {actual_qps:.2f})")
            logging.info(f"num of requests sent: {num_requests_sent}")
            logging.info(f"num of requests completed: {sum(completion_map.values())}")
            logging.info(f"Completion ratio: {sum(completion_map.values()) / num_requests_sent * 100:.2f}%")
            logging.info(f"Output file: {args.output_file_path}")
        except Exception as e:
            logging.error(f"Benchmark failed: {str(e)}")
            raise
        # finally:
        #     # Stop monitoring
        #     monitor_task.cancel()
        #     try:
        #         await monitor_task
        #     except asyncio.CancelledError:
        #         pass


## Old
async def benchmark(endpoint, model, api_key, workload_path, output_file_path):
    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=endpoint + "/v1/completions",
    )
    logging.info(f"Writing output to {output_file_path}")
    load_struct = load_workload(workload_path)
    with open(output_file_path, 'a', encoding='utf-8') as output_file:
        base_time = time.time()
        num_requests = 0
        batch_tasks = []
        idx = 0
        for requests_dict in load_struct:
            idx += 1
            ts = int(requests_dict["timestamp"])
            requests = requests_dict["requests"]
            cur_time = time.time()
            target_time = base_time + ts / 1000.0
            logging.warning(f"Prepare to launch {len(requests)} tasks after {target_time - cur_time}")
            if target_time > cur_time:
                await asyncio.sleep(target_time - cur_time)
                logging.info(f"batch idx: {idx}, sleeping for {target_time - cur_time}s")
            formatted_prompts = [wrap_prompt_as_chat_message(request["prompt"]) for request in requests]
            logging.info(f"batch idx: {idx}, num_requests: {len(formatted_prompts)}, time: {time.time()-base_time:.2f}s")
            for formatted_prompt in formatted_prompts:
                task = asyncio.create_task(
                    send_request(client, model, endpoint, formatted_prompt, output_file)
                )
                batch_tasks.append(task)
            num_requests += len(requests)
        await asyncio.gather(*batch_tasks)
        logging.warning(f"All {num_requests} requests completed for deployment.")


def main(args):
    logging.info(f"Starting benchmark on endpoint {args.endpoint}")
    start_time = time.time()
    # asyncio.run(benchmark(args.endpoint, args.model, args.api_key, args.workload_path, args.output_file_path))
    # asyncio.run(new_benchmark(args.endpoint, args.model, args.api_key, args.workload_path, args.output_file_path))
    # asyncio.run(new_benchmark_prescheduling(args.endpoint, args.model, args.api_key, args.workload_path, args.output_file_path))
    asyncio.run(new_benchmark_prescheduling2(args))
    end_time = time.time()
    logging.info(f"Benchmark completed in {end_time - start_time:.2f} seconds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Workload Generator')
    parser.add_argument("--workload-path", type=str, default=None, help="File path to the workload file.")
    parser.add_argument('--endpoint', type=str, required=True)
    parser.add_argument("--model", type=str, required=True, help="Name of the model.")
    parser.add_argument("--api-key", type=str, required=True, help="API key to the service. ")
    parser.add_argument('--output-dir', type=str, required=True)
    parser.add_argument('--output-file-path', type=str, default="output.jsonl")
    parser.add_argument('--routing-strategy', type=str, default="least-request")
    # parser.add_argument('--target_avg_rps', type=int, default=5, help="Target average RPS")

    args = parser.parse_args()
    main(args)
