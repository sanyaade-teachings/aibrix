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

package metrics

import (
	"context"
	"time"

	v1 "k8s.io/api/core/v1"

	autoscaling "k8s.io/api/autoscaling/v2"
)

// PodMetric contains pod metric value (the metric values are expected to be the metric as a milli-value)
type PodMetric struct {
	Timestamp       time.Time
	Window          time.Duration
	Value           int64
	MetricsName     string
	containerPort   int32
	ScaleObjectName string
}

// PodMetricsInfo contains pod metrics as a map from pod names to PodMetricsInfo
type PodMetricsInfo map[string]PodMetric

// MetricsClient knows how to query a remote interface to retrieve container-level
// resource metrics as well as pod-level arbitrary metrics
type MetricsClient interface {
	// GetPodContainerMetric gets the given resource metric (and an associated oldest timestamp)
	// for the specified named container in specific pods in the given namespace and when
	// the container is an empty string it returns the sum of all the container metrics.
	GetPodContainerMetric(ctx context.Context, metricName string, pod v1.Pod, containerPort int) (PodMetricsInfo, time.Time, error)

	// GetObjectMetric gets the given metric (and an associated timestamp) for the given
	// object in the given namespace, it can be used to fetch any object metrics supports /scale interface
	GetObjectMetric(ctx context.Context, metricName string, namespace string, objectRef *autoscaling.CrossVersionObjectReference, containerPort int) (PodMetricsInfo, time.Time, error)
}
