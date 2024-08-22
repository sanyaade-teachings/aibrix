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

package podautoscaler

import (
	"context"
	"fmt"
	"time"

	"github.com/aibrix/aibrix/pkg/controller/podautoscaler/scaler"

	autoscalingv2 "k8s.io/api/autoscaling/v2"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client/apiutil"

	corev1 "k8s.io/api/core/v1"

	autoscalingv1alpha1 "github.com/aibrix/aibrix/api/autoscaling/v1alpha1"
	podutils "github.com/aibrix/aibrix/pkg/utils"
	autoscalingv1 "k8s.io/api/autoscaling/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// Add creates a new PodAutoscaler Controller and adds it to the Manager with default RBAC.
// The Manager will set fields on the Controller and Start it when the Manager is Started.
func Add(mgr manager.Manager) error {
	r, err := newReconciler(mgr)
	if err != nil {
		return err
	}
	return add(mgr, r)
}

// newReconciler returns a new reconcile.Reconciler
func newReconciler(mgr manager.Manager) (reconcile.Reconciler, error) {
	// Instantiate a new PodAutoscalerReconciler with the given manager's client and scheme
	reconciler := &PodAutoscalerReconciler{
		Client:        mgr.GetClient(),
		Scheme:        mgr.GetScheme(),
		EventRecorder: mgr.GetEventRecorderFor("PodAutoscaler"),
		Mapper:        mgr.GetRESTMapper(),
	}
	return reconciler, nil
}

// add adds a new Controller to mgr with r as the reconcile.Reconciler
func add(mgr manager.Manager, r reconcile.Reconciler) error {
	// Create a new controller managed by AIBrix manager, watching for changes to PodAutoscaler objects
	// and HorizontalPodAutoscaler objects.
	err := ctrl.NewControllerManagedBy(mgr).
		For(&autoscalingv1alpha1.PodAutoscaler{}).
		Watches(&autoscalingv2.HorizontalPodAutoscaler{}, &handler.EnqueueRequestForObject{}).
		Complete(r)

	klog.V(4).InfoS("Added AIBricks pod-autoscaler-controller successfully")
	return err
}

var _ reconcile.Reconciler = &PodAutoscalerReconciler{}

// PodAutoscalerReconciler reconciles a PodAutoscaler object
type PodAutoscalerReconciler struct {
	client.Client
	Scheme        *runtime.Scheme
	EventRecorder record.EventRecorder
	Mapper        apimeta.RESTMapper
	Autoscaler    scaler.Scaler
}

//+kubebuilder:rbac:groups=autoscaling.aibrix.ai,resources=podautoscalers,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=autoscaling.aibrix.ai,resources=podautoscalers/status,verbs=get;update;patch
//+kubebuilder:rbac:groups=autoscaling.aibrix.ai,resources=podautoscalers/finalizers,verbs=update
//+kubebuilder:rbac:groups=autoscaling,resources=horizontalpodautoscalers,verbs=get;list;watch;create;update;patch;delete

// Reconcile is part of the main Kubernetes reconciliation loop which aims to
// move the current state of the cluster closer to the desired state as specified by
// the PodAutoscaler resource. It handles the creation, update, and deletion logic for
// HorizontalPodAutoscalers based on the PodAutoscaler specifications.
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.17.3/pkg/reconcile
func (r *PodAutoscalerReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	// Implement a timeout for the reconciliation process.
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	klog.V(3).InfoS("Reconciling PodAutoscaler", "requestName", req.NamespacedName)

	var pa autoscalingv1alpha1.PodAutoscaler
	if err := r.Get(ctx, req.NamespacedName, &pa); err != nil {
		if errors.IsNotFound(err) {
			// Object might have been deleted after reconcile request, ignore and return.
			klog.InfoS("PodAutoscaler resource not found. Ignoring since object must have been deleted")
			return ctrl.Result{}, nil
		}
		klog.ErrorS(err, "Failed to get PodAutoscaler")
		return ctrl.Result{}, err
	}

	switch pa.Spec.ScalingStrategy {
	case autoscalingv1alpha1.HPA:
		return r.reconcileHPA(ctx, pa)
	case autoscalingv1alpha1.KPA:
		return r.reconcileKPA(ctx, pa)
	case autoscalingv1alpha1.APA:
		return r.reconcileAPA(ctx, pa)
	default:
		return ctrl.Result{}, fmt.Errorf("unknown autoscaling strategy: %s", pa.Spec.ScalingStrategy)
	}
}

func (r *PodAutoscalerReconciler) reconcileHPA(ctx context.Context, pa autoscalingv1alpha1.PodAutoscaler) (ctrl.Result, error) {
	// Generate a corresponding HorizontalPodAutoscaler
	hpa := makeHPA(&pa)
	hpaName := types.NamespacedName{
		Name:      hpa.Name,
		Namespace: hpa.Namespace,
	}

	existingHPA := autoscalingv2.HorizontalPodAutoscaler{}
	err := r.Get(ctx, hpaName, &existingHPA)
	if err != nil && errors.IsNotFound(err) {
		// HPA does not exist, create a new one.
		klog.InfoS("Creating a new HPA", "HPA.Namespace", hpa.Namespace, "HPA.Name", hpa.Name)
		if err = r.Create(ctx, hpa); err != nil {
			klog.ErrorS(err, "Failed to create new HPA")
			return ctrl.Result{}, err
		}
	} else if err != nil {
		// Error occurred while fetching the existing HPA, report the error and requeue.
		klog.ErrorS(err, "Failed to get HPA")
		return ctrl.Result{}, err
	} else {
		// Update the existing HPA if it already exists.
		klog.InfoS("Updating existing HPA", "HPA.Namespace", existingHPA.Namespace, "HPA.Name", existingHPA.Name)

		err = r.Update(ctx, hpa)
		if err != nil {
			klog.ErrorS(err, "Failed to update HPA")
			return ctrl.Result{}, err
		}
	}

	// TODO: add status update. Currently, actualScale and desireScale are not synced from HPA object yet.
	// Return with no error and no requeue needed.
	return ctrl.Result{}, nil
}

func (r *PodAutoscalerReconciler) reconcileKPA(ctx context.Context, pa autoscalingv1alpha1.PodAutoscaler) (ctrl.Result, error) {
	paStatusOriginal := pa.Status.DeepCopy()
	scaleReference := fmt.Sprintf("%s/%s/%s", pa.Spec.ScaleTargetRef.Kind, pa.Namespace, pa.Spec.ScaleTargetRef.Name)

	targetGV, err := schema.ParseGroupVersion(pa.Spec.ScaleTargetRef.APIVersion)
	if err != nil {
		r.EventRecorder.Event(&pa, corev1.EventTypeWarning, "FailedGetScale", err.Error())
		// TODO: convert conditionType to type instead of using string
		setCondition(&pa, "AbleToScale", metav1.ConditionFalse, "FailedGetScale", "the PodAutoscaler controller was unable to get the target's current scale: %v", err)
		if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
			utilruntime.HandleError(err)
		}
		return ctrl.Result{}, fmt.Errorf("invalid API version in scale target reference: %v", err)
	}

	targetGK := schema.GroupKind{
		Group: targetGV.Group,
		Kind:  pa.Spec.ScaleTargetRef.Kind,
	}
	mappings, err := r.Mapper.RESTMappings(targetGK)
	if err != nil {
		r.EventRecorder.Event(&pa, corev1.EventTypeWarning, "FailedGetScale", err.Error())
		setCondition(&pa, "AbleToScale", metav1.ConditionFalse, "FailedGetScale", "the HPA controller was unable to get the target's current scale: %v", err)
		if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
			utilruntime.HandleError(err)
		}
		return ctrl.Result{}, fmt.Errorf("unable to determine resource for scale target reference: %v", err)
	}

	// TODO: retrieval targetGR for future scale update
	scale, targetGR, err := r.scaleForResourceMappings(ctx, pa.Namespace, pa.Spec.ScaleTargetRef.Name, mappings)
	if err != nil {
		r.EventRecorder.Event(&pa, corev1.EventTypeWarning, "FailedGetScale", err.Error())
		setCondition(&pa, "AbleToScale", metav1.ConditionFalse, "FailedGetScale", "the HPA controller was unable to get the target's current scale: %v", err)
		if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
			utilruntime.HandleError(err)
		}
		return ctrl.Result{}, fmt.Errorf("failed to query scale subresource for %s: %v", scaleReference, err)
	}

	setCondition(&pa, "AbleToScale", metav1.ConditionTrue, "SucceededGetScale", "the HPA controller was able to get the target's current scale")

	// current scale's replica count
	currentReplicas := scale.Spec.Replicas
	// desired replica count
	desiredReplicas := int32(0)
	rescaleReason := ""
	var minReplicas int32
	// minReplica is optional
	if pa.Spec.MinReplicas != nil {
		minReplicas = *pa.Spec.MinReplicas
	} else {
		minReplicas = 1
	}

	rescale := true
	logger := klog.FromContext(ctx)

	if scale.Spec.Replicas == int32(0) && minReplicas != 0 {
		// if the replica is 0, then we should not enable autoscaling
		desiredReplicas = 0
		rescale = false
	} else if currentReplicas > pa.Spec.MaxReplicas {
		desiredReplicas = pa.Spec.MaxReplicas
	} else if currentReplicas < minReplicas {
		desiredReplicas = minReplicas
	} else {
		// TODO: fix the compute replicas interface.
		metricDesiredReplicas, metricName, metricTimestamp, err := r.computeReplicasForMetrics(ctx, pa, scale)
		if err != nil && metricDesiredReplicas == -1 {
			r.setCurrentReplicasAndMetricsInStatus(&pa, currentReplicas)
			if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
				return ctrl.Result{}, fmt.Errorf("failed to update the resource status")
			}
			r.EventRecorder.Event(&pa, corev1.EventTypeWarning, "FailedComputeMetricsReplicas", err.Error())
			return ctrl.Result{}, fmt.Errorf("failed to compute desired number of replicas based on listed metrics for %s: %v", scaleReference, err)
		}

		logger.V(4).Info("Proposing desired replicas",
			"desiredReplicas", metricDesiredReplicas,
			"metric", metricName,
			"timestamp", metricTimestamp,
			"scaleTarget", scaleReference)

		rescaleMetric := ""
		if metricDesiredReplicas > desiredReplicas {
			desiredReplicas = metricDesiredReplicas
			rescaleMetric = metricName
		}
		if desiredReplicas > currentReplicas {
			rescaleReason = fmt.Sprintf("%s above target", rescaleMetric)
		}
		if desiredReplicas < currentReplicas {
			rescaleReason = "All metrics below target"
		}
		rescale = desiredReplicas != currentReplicas
	}

	if rescale {
		scale.Spec.Replicas = desiredReplicas
		r.EventRecorder.Eventf(&pa, corev1.EventTypeWarning, "FailedRescale", "New size: %d; reason: %s; error: %v", desiredReplicas, rescaleReason, err.Error())
		setCondition(&pa, "AbleToScale", metav1.ConditionFalse, "FailedUpdateScale", "the HPA controller was unable to update the target scale: %v", err)
		r.setCurrentReplicasAndMetricsInStatus(&pa, currentReplicas)
		if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
			utilruntime.HandleError(err)
		}

		if err := r.updateScale(ctx, pa.Namespace, targetGR, scale); err != nil {
			return ctrl.Result{}, fmt.Errorf("failed to rescale %s: %v", scaleReference, err)
		}

		// which way to go?. not sure the best practice in controller-runtime
		//if err := r.Client.SubResource("scale").Update(ctx, scale); err != nil {
		//	return ctrl.Result{}, fmt.Errorf("failed to rescale %s: %v", scaleReference, err)
		//}

		logger.Info("Successfully rescaled",
			//"PodAutoscaler", klog.KObj(pa),
			"currentReplicas", currentReplicas,
			"desiredReplicas", desiredReplicas,
			"reason", rescaleReason)
	}

	if err := r.updateStatusIfNeeded(ctx, paStatusOriginal, &pa); err != nil {
		// we can overwrite retErr in this case because it's an internal error.
		return ctrl.Result{}, err
	}

	return ctrl.Result{}, nil
}

func (r *PodAutoscalerReconciler) reconcileAPA(ctx context.Context, pa autoscalingv1alpha1.PodAutoscaler) (ctrl.Result, error) {
	// scraper
	// decider

	return ctrl.Result{}, fmt.Errorf("not implemneted yet")
}

// scaleForResourceMappings attempts to fetch the scale for the resource with the given name and namespace,
// trying each RESTMapping in turn until a working one is found.  If none work, the first error is returned.
// It returns both the scale, as well as the group-resource from the working mapping.
func (r *PodAutoscalerReconciler) scaleForResourceMappings(ctx context.Context, namespace, name string, mappings []*apimeta.RESTMapping) (*autoscalingv1.Scale, schema.GroupResource, error) {
	var firstErr error
	for i, mapping := range mappings {
		targetGR := mapping.Resource.GroupResource()
		scale := &autoscalingv1.Scale{}
		key := client.ObjectKey{Namespace: namespace, Name: name}
		err := r.Get(ctx, key, scale)
		if err == nil {
			return scale, targetGR, nil
		}

		if firstErr == nil {
			firstErr = err
		}

		// if this is the first error, remember it,
		// then go on and try other mappings until we find a good one
		if i == 0 {
			firstErr = err
		}
	}

	// make sure we handle an empty set of mappings
	if firstErr == nil {
		firstErr = fmt.Errorf("unrecognized resource")
	}

	return nil, schema.GroupResource{}, firstErr
}

func (r *PodAutoscalerReconciler) updateScale(ctx context.Context, namespace string, targetGR schema.GroupResource, scale *autoscalingv1.Scale) error {
	// Get GVK
	gvk, err := apiutil.GVKForObject(scale, r.Client.Scheme())
	if err != nil {
		return err
	}

	// Get unstructured object
	scaleObj := &unstructured.Unstructured{}
	scaleObj.SetGroupVersionKind(gvk)
	scaleObj.SetNamespace(namespace)
	scaleObj.SetName(scale.Name)

	// Update scale object
	//err = r.Client.Patch(ctx, scale, client.Apply, client.FieldOwner("operator-name"))
	err = r.Client.Patch(ctx, scale, client.Apply)
	if err != nil {
		return err
	}

	return nil
}

// setCondition sets the specific condition type on the given HPA to the specified value with the given reason
// and message.  The message and args are treated like a format string.  The condition will be added if it is
// not present.
func setCondition(hpa *autoscalingv1alpha1.PodAutoscaler, conditionType string, status metav1.ConditionStatus, reason, message string, args ...interface{}) {
	hpa.Status.Conditions = podutils.SetConditionInList(hpa.Status.Conditions, conditionType, status, reason, message, args...)
}

// setCurrentReplicasAndMetricsInStatus sets the current replica count and metrics in the status of the HPA.
func (a *PodAutoscalerReconciler) setCurrentReplicasAndMetricsInStatus(pa *autoscalingv1alpha1.PodAutoscaler, currentReplicas int32) {
	a.setStatus(pa, currentReplicas, pa.Status.DesiredScale, false)
}

// setStatus recreates the status of the given HPA, updating the current and
// desired replicas, as well as the metric statuses
func (a *PodAutoscalerReconciler) setStatus(pa *autoscalingv1alpha1.PodAutoscaler, currentReplicas, desiredReplicas int32, rescale bool) {
	pa.Status = autoscalingv1alpha1.PodAutoscalerStatus{
		ActualScale:   currentReplicas,
		DesiredScale:  desiredReplicas,
		LastScaleTime: pa.Status.LastScaleTime,
		Conditions:    pa.Status.Conditions,
	}

	if rescale {
		now := metav1.NewTime(time.Now())
		pa.Status.LastScaleTime = &now
	}
}

func (r *PodAutoscalerReconciler) updateStatusIfNeeded(ctx context.Context, oldStatus *autoscalingv1alpha1.PodAutoscalerStatus, newPA *autoscalingv1alpha1.PodAutoscaler) error {
	// skip status update if the status is not exact same
	if apiequality.Semantic.DeepEqual(oldStatus, newPA.Status) {
		return nil
	}
	return r.updateStatus(ctx, newPA)
}

// updateStatus actually does the update request for the status of the given HPA
func (r *PodAutoscalerReconciler) updateStatus(ctx context.Context, pa *autoscalingv1alpha1.PodAutoscaler) error {
	if err := r.Status().Update(ctx, pa); err != nil {
		r.EventRecorder.Event(pa, corev1.EventTypeWarning, "FailedUpdateStatus", err.Error())
		return fmt.Errorf("failed to update status for %s: %v", pa.Name, err)
	}
	logger := klog.FromContext(ctx)
	logger.V(2).Info("Successfully updated status", "PodAutoscaler", klog.KObj(pa))
	return nil
}

// computeReplicasForMetrics computes the desired number of replicas for the metric specifications listed in the pod autoscaler,
// returning the maximum of the computed replica counts, a description of the associated metric, and the statuses of
// all metrics computed.
// It may return both valid metricDesiredReplicas and an error,
// when some metrics still work and HPA should perform scaling based on them.
// If PodAutoscaler cannot do anything due to error, it returns -1 in metricDesiredReplicas as a failure signal.
func (r *PodAutoscalerReconciler) computeReplicasForMetrics(ctx context.Context, pa autoscalingv1alpha1.PodAutoscaler, scale *autoscalingv1.Scale) (replicas int32, metrics string, timestamp time.Time, err error) {
	currentTimestamp := time.Now()
	scaleResult := r.Autoscaler.Scale(0, 0, currentTimestamp)
	if scaleResult.ScaleValid {
		return scaleResult.DesiredPodCount, "", currentTimestamp, nil
	}

	return 0, "", currentTimestamp, fmt.Errorf("can not calculate metrics for scale %s", scale.Name)
}
