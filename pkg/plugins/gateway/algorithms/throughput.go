/*
Copyright 2024 The Aibrix Team.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package routingalgorithms

import (
	"context"
	"fmt"
	"math"

	"github.com/aibrix/aibrix/pkg/cache"
	v1 "k8s.io/api/core/v1"
	"k8s.io/klog/v2"
)

type throughputRouter struct {
	cache *cache.Cache
}

func NewThroughputRouter() Router {
	cache, err := cache.GetCache()
	if err != nil {
		panic(err)
	}

	return throughputRouter{
		cache: cache,
	}
}

func (r throughputRouter) Route(ctx context.Context, pods map[string]*v1.Pod) (string, error) {
	var targetPodIP string
	minCount := math.MaxFloat64

	if len(pods) == 0 {
		return "", fmt.Errorf("no pods to forward request")
	}

	for _, pod := range pods {
		if pod.Status.PodIP == "" {
			continue
		}

		promptThroughput, err := r.cache.GetPodMetric(pod.Name, avg_prompt_throughput_toks_per_s)
		if err != nil {
			klog.Error(err)
			continue
		}
		generationThroughput, err := r.cache.GetPodMetric(pod.Name, avg_generation_throughput_toks_per_s)
		if err != nil {
			klog.Error(err)
			continue
		}

		// processing prompt tokens is twice as expensive than generation tokens
		total_throughput := 2*promptThroughput.Value + generationThroughput.Value
		klog.V(4).Infof("pod: %v, podIP: %v, promptThroughput: %v, generationThroughput: %v, total_throughput: %v",
			pod.Name, pod.Status.PodIP, promptThroughput, generationThroughput, total_throughput)

		if total_throughput <= minCount {
			minCount = total_throughput
			targetPodIP = pod.Status.PodIP
		}
	}

	if targetPodIP == "" {
		return "", fmt.Errorf("no pods to forward request")
	}

	return targetPodIP + ":" + podMetricPort, nil
}
