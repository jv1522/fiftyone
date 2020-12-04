"""
PyTorch utilities.

| Copyright 2017-2020, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import inspect
import logging

import numpy as np
from PIL import Image

from eta.core.config import Config
import eta.core.learning as etal
import eta.core.utils as etau

import fiftyone.core.labels as fol
import fiftyone.core.models as fom
import fiftyone.core.utils as fou
from fiftyone.zoo.models import HasZooModel

fou.ensure_torch()
import torch
import torchvision
from torchvision.transforms import functional as F
from torch.utils.data import Dataset


logger = logging.getLogger(__name__)


def _make_data_loader(
    sample_collection, transforms, force_rgb=True, batch_size=16, num_workers=4
):
    image_paths = []
    for sample in sample_collection.select_fields():
        image_paths.append(sample.filepath)

    dataset = TorchImageDataset(
        image_paths, transform=transforms, force_rgb=force_rgb
    )

    return torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, num_workers=num_workers
    )


class TorchModelConfig(Config, HasZooModel):
    """Configuration for running a Torch model.

    The model's state dict can be specified via ``model_name`` and/or
    ``model_path``, or it can be indirectly loaded when ``entrypoint_fcn`` is
    called.

    Args:
        model_name: the name of a zoo model containing a state dict to load
        model_path: the path to a state dict on disk to load
        entrypoint_fcn: a string like ``"torchvision.models.inception_v3"``
            specifying the entrypoint function that loads the model
        entrypoint_args: a dictionary of arguments for ``entrypoint_fcn``
        output_processor_cls: a string like
            ``"fifytone.utils.torch.ClassifierOutputProcessor"`` specifying the
            :class:`fifytone.utils.torch.OutputProcessor` to use
        output_processor_args: a dictionary of arguments for
            ``output_processor_cls(class_labels, **kwargs)``
        labels_string (None): a comma-separated list of the class-names in the
            classifier, ordered in accordance with the trained model
        labels_path (None): the path to the labels map for the model
        use_half_precision (None): whether to use half precision
        image_min_size (None): a minimum ``(width, height)`` to which to resize
            the input images during preprocessing
        image_min_dim (None): a minimum image dimension to which to resize the
            input images during preprocessing
        image_size (None): a ``(width, height)`` to which to resize the input
            images during preprocessing
        image_dim (None): resize the smaller input dimension to this value
            during preprocessing
        image_mean (None): a 3-array of mean values in ``[0, 1]`` for
            preprocessing the input images
        image_std: a 3-array of std values in ``[0, 1]`` for preprocessing the
            input images
    """

    def __init__(self, d):
        d = self.init(d)

        self.entrypoint_fcn = self.parse_string(d, "entrypoint_fcn")
        self.entrypoint_args = self.parse_dict(
            d, "entrypoint_args", default=None
        )
        self.output_processor_cls = self.parse_string(
            d, "output_processor_cls"
        )
        self.output_processor_args = self.parse_dict(
            d, "output_processor_args", default=None
        )
        self.labels_string = self.parse_string(
            d, "labels_string", default=None
        )
        self.labels_path = self.parse_string(d, "labels_path", default=None)
        self.use_half_precision = self.parse_bool(
            d, "use_half_precision", default=None
        )
        self.image_min_size = self.parse_array(
            d, "image_min_size", default=None
        )
        self.image_min_dim = self.parse_number(
            d, "image_min_dim", default=None
        )
        self.image_size = self.parse_array(d, "image_size", default=None)
        self.image_dim = self.parse_number(d, "image_dim", default=None)
        self.image_mean = self.parse_array(d, "image_mean", default=None)
        self.image_std = self.parse_array(d, "image_std", default=None)


class TorchModel(fom.Model):
    """Wrapper for evaluating a Torch model.

    Args:
        config: an :class:`TorchModelConfig`
    """

    def __init__(self, config):
        self.config = config

        # Get class labels
        self._class_labels = self._get_class_labels(config)
        self._num_classes = len(self._class_labels)

        # Build transforms
        self._transforms = self._build_transforms(config)

        # Load model
        self._use_gpu = torch.cuda.is_available()
        self._device = torch.device("cuda:0" if self._use_gpu else "cpu")
        self._use_half_precision = self.config.use_half_precision is True
        self._model = self._load_model(config)

        # Build output processor
        self._output_processor = self._build_output_processor(config)

    @property
    def use_gpu(self):
        """Whether the model is using GPU."""
        return self._use_gpu

    @property
    def device(self):
        """The ``torch.device`` that the model is using."""
        return self._device

    @property
    def use_half_precision(self):
        """Whether the model is using half precision."""
        return self._use_half_precision

    @property
    def transforms(self):
        """The ``torchvision.transforms`` that will/must be applied to each
        input before prediction.
        """
        return self._transforms

    @property
    def num_classes(self):
        """The number of classes for the model."""
        return self._num_classes

    @property
    def class_labels(self):
        """The list of class labels for the model."""
        return self._class_labels

    def predict(self, img):
        """Computes the prediction on a single image.

        If a Torch Tensor is provided, it is assumed that preprocessing has
        already been applied.

        Args:
            img: a PIL image (HWC), a numpy array (HWC) or Torch tensor (CHW)

        Returns:
            the prediction
        """
        if isinstance(img, torch.Tensor):
            imgs = img.unsqueeze(0)  # NCHW
        else:
            imgs = [img]  # NHWC

        return self.predict_all(imgs)[0]

    def predict_all(self, imgs):
        """Computes predictions for the tensor of images.

        If a Torch Tensor is provided, it is assumed that preprocessing has
        already been applied.

        Args:
            imgs: a list of PIL or numpy images (HWC), a numpy array of images
                (NHWC), or a Torch tensor (NCHW)

        Returns:
            a list of predictions
        """
        imgs, frame_size = self._preprocess_batch(imgs)

        if self._use_gpu:
            imgs = imgs.cuda()

        if self._use_half_precision:
            imgs = imgs.half()

        output = self._model(imgs)

        return self._output_processor(output, frame_size)

    def _preprocess_batch(self, imgs):
        if not isinstance(imgs, torch.Tensor):
            imgs = torch.stack([self._transforms(img) for img in imgs])

        height, width = list(imgs.size())[-2:]
        frame_size = (width, height)

        return imgs, frame_size

    def _get_class_labels(self, config):
        if config.labels_string:
            return config.labels_string.split(",")

        if not config.labels_path:
            raise ValueError(
                "Either `labels_string` or `labels_path` must be specified"
            )

        labels_path = fou.fill_patterns(config.labels_path)
        labels_map = etal.load_labels_map(labels_path)
        return etal.get_class_labels(labels_map)

    def _build_transforms(self, config):
        transforms = [torchvision.transforms.ToPILImage()]

        if config.image_min_size:
            transforms.append(MinResize(config.image_min_size))
        elif config.image_min_dim:
            transforms.append(MinResize(config.image_min_dim))
        elif config.image_size:
            transforms.append(torchvision.transforms.Resize(config.image_size))
        elif config.image_dim:
            transforms.append(torchvision.transforms.Resize(config.image_dim))

        # Converts PIL/numpy (HWC) to Torch tensor (CHW) in [0, 1]
        transforms.append(torchvision.transforms.ToTensor())

        if config.image_mean or config.image_std:
            if not config.image_mean or not config.image_std:
                raise ValueError(
                    "Both `image_mean` and `image_std` must be provided"
                )

            transforms.append(
                torchvision.transforms.Normalize(
                    config.image_mean, config.image_std
                )
            )

        return torchvision.transforms.Compose(transforms)

    def _load_model(self, config):
        # Load model
        entrypoint = etau.get_function(config.entrypoint_fcn)
        kwargs = config.entrypoint_args or {}
        model = entrypoint(**kwargs)
        model.to(self._device)

        # Load state dict, if necessary
        # This may not be necessary if the entrypoint loads the model
        if config.model_name or config.model_path:
            config.download_model_if_necessary()
            state_dict = torch.load(
                config.model_path, map_location=self._device
            )
            model.load_state_dict(state_dict)

        if self._use_half_precision:
            model = model.half()

        model.train(False)

        return model

    def _build_output_processor(self, config):
        output_processor_cls = etau.get_class(config.output_processor_cls)
        kwargs = config.output_processor_args or {}
        return output_processor_cls(self._class_labels, **kwargs)


class MinResize(object):
    """Transform that resizes the PIL image or torch Tensor, if necessary, so
    that its minimum dimensions are at least the specified size.

    Args:
        min_output_size: Desired minimum output dimensions. Can either be a
            ``(min_height, min_width)`` tuple or a single ``min_dim``
        interpolation (None): Optional interpolation mode. Passed directly to
            ``torchvision.transforms.functional.resize``
    """

    def __init__(self, min_output_size, interpolation=None):
        if isinstance(min_output_size, int):
            min_output_size = (min_output_size, min_output_size)

        self.min_output_size = min_output_size
        self.interpolation = interpolation

        self._kwargs = {}
        if interpolation is not None:
            self._kwargs["interpolation"] = interpolation

    def __call__(self, pil_image_or_tensor):
        if isinstance(pil_image_or_tensor, torch.Tensor):
            h, w = list(pil_image_or_tensor.size())[-2:]
        else:
            w, h = pil_image_or_tensor.size

        minh, minw = self.min_output_size

        if h >= minh and w >= minw:
            return pil_image_or_tensor

        alpha = max(minh / h, minw / w)
        size = (int(round(alpha * h)), int(round(alpha * w)))
        return F.resize(pil_image_or_tensor, size, **self._kwargs)


class OutputProcessor(object):
    """Interface for processing the outputs of Torch models."""

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: the model output for the batch of predictions
            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Label` instances
        """
        raise NotImplementedError("subclass must implement __call__")


class ClassifierOutputProcessor(OutputProcessor):
    """Output processor for single label classifiers.

    Args:
        class_labels: the list of class labels for the model
        confidence_thresh (None): an optional confidence threshold to apply
            when deciding whether to keep predictions
    """

    def __init__(self, class_labels, confidence_thresh=None):
        self.class_labels = class_labels
        self.confidence_thresh = confidence_thresh

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: a ``FloatTensor[N, M]`` containing the logits for ``N``
                images and ``M`` classes
            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Classification` instances
        """
        logits = output.detach().cpu().numpy()

        predictions = np.argmax(logits, axis=1)
        odds = np.exp(logits)
        odds /= np.sum(odds, axis=1, keepdims=True)
        scores = np.max(odds, axis=1)

        preds = []
        for prediction, score in zip(predictions, scores):
            if (
                self.confidence_thresh is not None
                and score < self.confidence_thresh
            ):
                classification = None
            else:
                classification = fol.Classification(
                    label=self.class_labels[prediction], confidence=score,
                )

            preds.append(classification)

        return preds


class DetectorOutputProcessor(OutputProcessor):
    """Output processor for object detectors.

    Args:
        class_labels: the list of class labels for the model
        confidence_thresh (None): an optional confidence threshold to apply
            when deciding whether to keep predictions
    """

    def __init__(self, class_labels, confidence_thresh=None):
        self.class_labels = class_labels
        self.confidence_thresh = confidence_thresh

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: a batch of predictions ``output = List[Dict[Tensor]]``,
                where each dict has the following keys:

                -   boxes (``FloatTensor[N, 4]``): the predicted boxes in
                    ``[x1, y1, x2, y2]`` format (absolute coordinates)
                -   labels (``Int64Tensor[N]``): the predicted labels
                -   scores (``Tensor[N]``): the scores for each prediction

            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Detections` instances
        """
        return [self._parse_detections(o, frame_size) for o in output]

    def _parse_detections(self, output, frame_size):
        width, height = frame_size

        boxes = output["boxes"].detach().cpu().numpy()
        labels = output["labels"].detach().cpu().numpy()
        scores = output["scores"].detach().cpu().numpy()

        detections = []
        for box, label, score in zip(boxes, labels, scores):
            if (
                self.confidence_thresh is not None
                and score < self.confidence_thresh
            ):
                continue

            x1, y1, x2, y2 = box
            bounding_box = [
                x1 / width,
                y1 / height,
                (x2 - x1) / width,
                (y2 - y1) / height,
            ]

            detections.append(
                fol.Detection(
                    label=self.class_labels[label],
                    bounding_box=bounding_box,
                    confidence=score,
                )
            )

        return fol.Detections(detections=detections)


class InstanceSegmenterOutputProcessor(OutputProcessor):
    """Output processor for instance segementers.

    Args:
        class_labels: the list of class labels for the model
        confidence_thresh (None): an optional confidence threshold to apply
            when deciding whether to keep predictions
        mask_thresh (0.5): a threshold to use to convert soft masks to binary
            masks
    """

    def __init__(self, class_labels, confidence_thresh=None, mask_thresh=0.5):
        self.class_labels = class_labels
        self.confidence_thresh = confidence_thresh
        self.mask_thresh = mask_thresh

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: a batch of predictions ``output = List[Dict[Tensor]]``,
                where each dict has the following keys:

                -   boxes (``FloatTensor[N, 4]``): the predicted boxes in
                    ``[x1, y1, x2, y2]`` format (absolute coordinates)
                -   labels (``Int64Tensor[N]``): the predicted labels
                -   scores (``Tensor[N]``): the scores for each prediction
                -   masks (``FloatTensor[N, 1, H, W]``): the predicted masks
                    for each instance, in ``[0, 1]``

            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Detections` instances
        """
        return [self._parse_detections(o, frame_size) for o in output]

    def _parse_detections(self, output, frame_size):
        width, height = frame_size

        boxes = output["boxes"].detach().cpu().numpy()
        labels = output["labels"].detach().cpu().numpy()
        scores = output["scores"].detach().cpu().numpy()
        masks = output["masks"].detach().cpu().numpy()

        detections = []
        for box, label, score, soft_mask in zip(boxes, labels, scores, masks):
            if (
                self.confidence_thresh is not None
                and score < self.confidence_thresh
            ):
                continue

            x1, y1, x2, y2 = box
            bounding_box = [
                x1 / width,
                y1 / height,
                (x2 - x1) / width,
                (y2 - y1) / height,
            ]

            soft_mask = np.squeeze(soft_mask, axis=0)[
                int(round(y1)) : int(round(y2)),
                int(round(x1)) : int(round(x2)),
            ]
            mask = soft_mask > self.mask_thresh

            detections.append(
                fol.Detection(
                    label=self.class_labels[label],
                    bounding_box=bounding_box,
                    mask=mask,
                    confidence=score,
                )
            )

        return fol.Detections(detections=detections)


class KeypointDetectorOutputProcessor(OutputProcessor):
    """Output processor for keypoint detection models.

    Args:
        class_labels: the list of class labels for the model
        edges (None): an optional list of list of vertices specifying polyline
            connections between keypoints to draw
        confidence_thresh (None): an optional confidence threshold to apply
            when deciding whether to keep predictions
    """

    def __init__(self, class_labels, edges=None, confidence_thresh=None):
        self.class_labels = class_labels
        self.edges = edges
        self.confidence_thresh = confidence_thresh

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: a batch of predictions ``output = List[Dict[Tensor]]``,
                where each dict has the following keys:

                -   boxes (``FloatTensor[N, 4]``): the predicted boxes in
                    ``[x1, y1, x2, y2]`` format (absolute coordinates)
                -   labels (``Int64Tensor[N]``): the predicted labels
                -   scores (``Tensor[N]``): the scores for each prediction
                -   keypoints (``FloatTensor[N, K, ...]``): the predicted
                    keypoints for each instance in ``[x, y, ...]`` format

            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Label` dicts
        """
        return [self._parse_detections(o, frame_size) for o in output]

    def _parse_detections(self, output, frame_size):
        width, height = frame_size

        boxes = output["boxes"].detach().cpu().numpy()
        labels = output["labels"].detach().cpu().numpy()
        scores = output["scores"].detach().cpu().numpy()
        keypoints = output["keypoints"].detach().cpu().numpy()

        _detections = []
        _keypoints = []
        _polylines = []
        for box, label, score, kpts in zip(boxes, labels, scores, keypoints):
            if (
                self.confidence_thresh is not None
                and score < self.confidence_thresh
            ):
                continue

            x1, y1, x2, y2 = box
            bounding_box = [
                x1 / width,
                y1 / height,
                (x2 - x1) / width,
                (y2 - y1) / height,
            ]

            points = [(p[0] / width, p[1] / height) for p in kpts]

            _detections.append(
                fol.Detection(
                    label=self.class_labels[label],
                    bounding_box=bounding_box,
                    confidence=score,
                )
            )

            _keypoints.append(
                fol.Keypoint(
                    label=self.class_labels[label],
                    points=points,
                    confidence=score,
                )
            )

            if self.edges is not None:
                _polylines.append(
                    fol.Polyline(
                        points=[[points[v] for v in e] for e in self.edges],
                        confidence=score,
                        closed=False,
                        filled=False,
                    )
                )

        label = {
            "detections": fol.Detections(detections=_detections),
            "keypoints": fol.Keypoints(keypoints=_keypoints),
        }

        if self.edges is not None:
            label["polylines"] = fol.Polylines(polylines=_polylines)

        return label


class SegmenterOutputProcessor(OutputProcessor):
    """Output processor for semantic segementers.

    Args:
        class_labels: the list of class labels for the model
    """

    def __init__(self, class_labels):
        self.class_labels = class_labels

    def __call__(self, output, frame_size):
        """Parses the model output.

        Args:
            output: a batch of predictions ``output = Dict[Tensor]``,
                where the dict has the following keys:

                -   out (``FloatTensor[N, M, H, W]``): the segmentation map
                    probabilities for the ``N`` images across the ``M`` classes

            frame_size: the ``(width, height)`` of the frames in the batch

        Returns:
            a list of :class:`fiftyone.core.labels.Segmentation` instances
        """
        probs = output["out"].detach().cpu().numpy()
        masks = probs.argmax(axis=1)
        return [fol.Segmentation(mask=mask) for mask in masks]


class TorchImageDataset(Dataset):
    """A ``torch.utils.data.Dataset`` of images.

    Instances of this class emit images, or ``(image, sample_id)`` pairs if
    ``sample_ids`` are provided.

    Args:
        image_paths: an iterable of image paths
        sample_ids (None): an iterable of :class:`fiftyone.core.sample.Sample`
            IDs corresponding to each image
        transform (None): an optional transform to apply to the images
        force_rgb (False): whether to force convert the images to RGB
    """

    def __init__(
        self, image_paths, sample_ids=None, transform=None, force_rgb=False
    ):
        self.image_paths = list(image_paths)
        self.sample_ids = list(sample_ids) if sample_ids else None
        self.transform = transform
        self.force_rgb = force_rgb

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        img = Image.open(self.image_paths[idx])

        if self.force_rgb:
            img = img.convert("RGB")

        if self.transform:
            img = self.transform(img)

        if self.has_sample_ids:
            # pylint: disable=unsubscriptable-object
            return img, self.sample_ids[idx]

        return img

    @property
    def has_sample_ids(self):
        """Whether this dataset has sample IDs."""
        return self.sample_ids is not None


class TorchImageClassificationDataset(Dataset):
    """A ``torch.utils.data.Dataset`` for image classification.

    Instances of this dataset emit images and their associated targets, either
    directly as ``(image, target)`` pairs or as ``(image, target, sample_id)``
    pairs if ``sample_ids`` are provided.

    Args:
        image_paths: an iterable of image paths
        targets: an iterable of targets
        sample_ids (None): an iterable of :class:`fiftyone.core.sample.Sample`
            IDs corresponding to each image
        transform (None): an optional transform to apply to the images
        force_rgb (False): whether to force convert the images to RGB
    """

    def __init__(
        self,
        image_paths,
        targets,
        sample_ids=None,
        transform=None,
        force_rgb=False,
    ):
        self.image_paths = list(image_paths)
        self.targets = list(targets)
        self.sample_ids = list(sample_ids) if sample_ids else None
        self.transform = transform
        self.force_rgb = force_rgb

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        img = Image.open(self.image_paths[idx])
        target = self.targets[idx]

        if self.force_rgb:
            img = img.convert("RGB")

        if self.transform:
            img = self.transform(img)

        if self.has_sample_ids:
            # pylint: disable=unsubscriptable-object
            return img, target, self.sample_ids[idx]

        return img, target

    @property
    def has_sample_ids(self):
        """Whether this dataset has sample IDs."""
        return self.sample_ids is not None


class TorchImagePatchesDataset(Dataset):
    """A ``torch.utils.data.Dataset`` of image patch tensors extracted from a
    list of images.

    Instances of this class emit Torch tensors containing the stacked
    (along axis 0) patches from each image, or ``(patch_tensor, sample_id)``
    pairs if ``sample_ids`` are provided.

    The provided ``transform`` must ensure that all image patches are resized
    to the same shape and formatted as torch Tensors so that they can be
    stacked.

    Args:
        image_paths: an iterable of image paths
        detections: an iterable of :class:`fiftyone.core.labels.Detections`
            instances specifying the image patch(es) to extract from each
            image
        transform: a torchvision transform to apply to each image patch
        sample_ids (None): an iterable of :class:`fiftyone.core.sample.Sample`
            IDs corresponding to each image
        force_rgb (False): whether to force convert the images to RGB
        force_square (False): whether to minimally manipulate the patch
            bounding boxes into squares prior to extraction
    """

    def __init__(
        self,
        image_paths,
        detections,
        transform,
        sample_ids=None,
        force_rgb=False,
        force_square=False,
    ):
        self.image_paths = list(image_paths)
        self.detections = list(detections)
        self.transform = transform
        self.sample_ids = list(sample_ids) if sample_ids else None
        self.force_rgb = force_rgb
        self.force_square = force_square

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        image_path = self.image_paths[idx]
        img = Image.open(image_path)

        if self.force_rgb:
            img = img.convert("RGB")

        detections = self.detections[idx].detections

        if not detections:
            raise ValueError(
                "No patches to extract from image '%s'" % image_path
            )

        img_patches = []
        for detection in detections:
            dobj = detection.to_detected_object()

            # @todo avoid PIL <-> numpy casts
            img_patch = dobj.bounding_box.extract_from(
                np.array(img), force_square=self.force_square
            )
            img_patch = Image.fromarray(img_patch)

            img_patch = self.transform(img_patch)

            img_patches.append(img_patch)

        img_patches = torch.stack(img_patches, dim=0)

        if self.has_sample_ids:
            # pylint: disable=unsubscriptable-object
            return img_patches, self.sample_ids[idx]

        return img_patches

    @property
    def has_sample_ids(self):
        """Whether this dataset has sample IDs."""
        return self.sample_ids is not None


def from_image_classification_dir_tree(dataset_dir):
    """Creates a ``torch.utils.data.Dataset`` for the given image
    classification dataset directory tree.

    The directory should have the following format::

        <dataset_dir>/
            <classA>/
                <image1>.<ext>
                <image2>.<ext>
                ...
            <classB>/
                <image1>.<ext>
                <image2>.<ext>
                ...

    Args:
        dataset_dir: the dataset directory

    Returns:
        a ``torchvision.datasets.ImageFolder``
    """
    return torchvision.datasets.ImageFolder(dataset_dir)
