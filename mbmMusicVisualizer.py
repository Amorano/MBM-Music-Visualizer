# MBM's Music Visualizer: The Visualizer
# Visualize a provided audio file.

# Imports
import librosa
import torch
import random
import math
import numpy as np
from tqdm import tqdm
from scipy.signal import resample

import comfy.samplers
from nodes import common_ksampler

# Classes
class MusicVisualizer:
    """
    Visualize a provided audio file.

    Returns a batch tuple of images.
    """
    # Class Constants
    SEED_MODE_FIXED = "fixed"
    SEED_MODE_RANDOM = "random"
    SEED_MODE_INCREASE = "increase"
    SEED_MODE_DECREASE = "decrease"

    LATENT_MODE_STATIC = "static"
    LATENT_MODE_INCREASE = "increase"
    LATENT_MODE_DECREASE = "decrease"
    LATENT_MODE_FLOW = "flow"
    LATENT_MODE_GAUSS = "guassian"

    FEAT_APPLY_METHOD_ADD = "add"
    FEAT_APPLY_METHOD_SUBTRACT = "subtract"

    RETURN_TYPES = ("LATENT", "FLOAT")
    RETURN_NAMES = ("LATENTS", "FPS")
    FUNCTION = "process"
    CATEGORY = "MBMnodes/MusicVisualizer"

    # Constructor
    def __init__(self):
        pass

    # ComfyUI Functions
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "audio": ("AUDIO", ),
                "prompts": ("PROMPT_SEQ", ),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}), # TODO: EVERYTHING must use the seed
                "latent_image": ("LATENT", ),
                "seed_mode": ([MusicVisualizer.SEED_MODE_FIXED, MusicVisualizer.SEED_MODE_RANDOM, MusicVisualizer.SEED_MODE_INCREASE, MusicVisualizer.SEED_MODE_DECREASE], ),
                "latent_mode": ([MusicVisualizer.LATENT_MODE_FLOW, MusicVisualizer.LATENT_MODE_STATIC, MusicVisualizer.LATENT_MODE_INCREASE, MusicVisualizer.LATENT_MODE_DECREASE, MusicVisualizer.LATENT_MODE_GAUSS], ),
                "intensity": ("FLOAT", {"default": 1.0}), # Muiltiplier for the audio features
                "hop_length": ("INT", {"default": 512}),
                "fps_target": ("FLOAT", {"default": 24, "min": -1, "max": 10000}), # Provide `<= 0` to use whatever audio sampling comes up with
                "image_limit": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}), # Provide `<= 0` to use whatever audio sampling comes up with
                "latent_mod_limit": ("FLOAT", {"default": 16, "min": -1, "max": 10000}), # The maximum variation that can occur to the latent based on the latent's mean value. Provide `<= 0` to have no limit

                # TODO: Move these into a KSamplerSettings node?
                # Also might be worth adding a KSamplerSettings to KSamplerInputs node that splits it all out to go into the standard KSampler when done here?
                "model": ("MODEL",),
                "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }

    def process(self,
        audio: tuple,
        prompts: torch.Tensor, # [num of prompt sets, 2, *conditioning tensor shape]
        seed: int,
        latent_image: dict[str, torch.Tensor],
        seed_mode: str,
        latent_mode: str,
        intensity: float,
        hop_length: int,
        fps_target: float,
        image_limit: int,
        latent_mod_limit: float,

        model, # What's a Model?
        steps: int,
        cfg: float,
        sampler_name: str,
        scheduler: str,
        denoise: float,
    ):
        ## Setup Calculations
        # Unpack the audio
        y, sr = audio

        # Calculate the duration of the audio
        duration = librosa.get_duration(y=y, sr=sr, hop_length=hop_length)
        # hopSeconds = hop_length / sr

        # Calculate tempo
        onset = librosa.onset.onset_strength(y=y, sr=sr)
        # tempo: np.ndarray = librosa.beat.tempo(onset_envelope=onset, sr=sr, hop_length=hop_length, aggregate=None)
        # tempo /= float(hop_length) # Idk, it puts it to a more reasonable range for image tensors
        tempo = self._normalizeArray(librosa.beat.tempo(onset_envelope=onset, sr=sr, hop_length=hop_length, aggregate=None))

        # Calculate the spectrogram
        spectro = librosa.feature.melspectrogram(
            y=y,
            sr=sr,
            n_mels=128,
            fmax=8000,
            hop_length=hop_length
        )

        # Calculate normalized mean power per hop
        spectroMean = np.mean(spectro, axis=0)

        # Calculate normalized power gradient per hop
        spectroGrad = self._normalizeArray(np.gradient(spectroMean), minVal=-1.0, maxVal=1.0)

        # Normalize the spectro mean
        spectroMean = self._normalizeArray(spectroMean)

        # Calculate pitch chroma for hops
        chroma = librosa.feature.chroma_cqt(
            y=y,
            sr=sr,
            hop_length=hop_length
        )

        # Sort pitch chroma
        chromaSort = np.argsort(np.mean(chroma, axis=1))[::-1]

        # Calculate the output FPS
        if fps_target <= 0:
            # Calculate framerate based on audio
            fps = len(tempo) / duration
        else:
            # Specific framerate to target
            fps = fps_target

            # Calculate desired frame count
            desiredFrames = round(fps * duration)

            # print("FRAMES", desiredFrames, fps, duration)
            # print("TEMPO PRE:", tempo.shape, len(tempo), np.min(tempo), np.max(tempo), np.mean(tempo))

            # Resample audio features to match desired frame count
            tempo = resample(tempo, desiredFrames)
            spectroMean = resample(spectroMean, desiredFrames)
            spectroGrad = resample(spectroGrad, desiredFrames)

            # print("TEMPO POST:", tempo.shape, len(tempo), np.min(tempo), np.max(tempo), np.mean(tempo))

        # print("TEMPO:", tempo.shape, len(tempo))
        # print("INPUT TENSOR:", latent_image["samples"], latent_image["samples"].shape)

        ## Generation
        # Set intial prompts
        promptPos = self._packPromptForComfy(prompts[0][0])
        promptNeg = self._packPromptForComfy(prompts[0][1])

        if prompts.shape[0] > 1:
            # Calculate linear interpolation between prompts
            # TODO: almost equal check to avoid interpolation if they're basically the same
            # TODO: Instead of linear interpolation, jump farther based on audio feature intensity
            promptSeqPos = None
            promptSeqNeg = None
            relDesiredFrames = math.ceil(desiredFrames / (prompts.shape[0] - 1))
            for i in range(prompts.shape[0] - 1):
                if promptSeqPos is None:
                    promptSeqPos = self.tensorLinspace(prompts[i][0], prompts[i + 1][0], relDesiredFrames)
                    promptSeqNeg = self.tensorLinspace(prompts[i][1], prompts[i + 1][1], relDesiredFrames)
                else:
                    promptSeqPos = torch.vstack((promptSeqPos, self.tensorLinspace(prompts[i][0], prompts[i + 1][0], relDesiredFrames)[1:]))
                    promptSeqNeg = torch.vstack((promptSeqNeg, self.tensorLinspace(prompts[i][1], prompts[i + 1][1], relDesiredFrames)[1:]))

            # Trim off the fat
            promptSeqPos = promptSeqPos[:desiredFrames]
            promptSeqNeg = promptSeqNeg[:desiredFrames]

        # Prepare latent output tensor
        outputTensor: torch.Tensor = None
        latentTensor = latent_image["samples"].clone()
        for i in (pbar := tqdm(range(desiredFrames), desc="Music Visualization")):
            # Calculate the latent tensor
            latentTensor = self._iterateLatentByMode(
                latentTensor,
                latent_mode,
                latent_mod_limit,
                intensity,
                tempo[i],
                spectroMean[i],
                spectroGrad[i],
                chromaSort
            )

            # modifiers.append(self._calcLatentModifier(intensity, tempo[i], spectroMean[i], spectroGrad[i], chromaSort))

            # print("LATENT TENSOR:", latentTensor, latentTensor.shape)
            # print("LATENT MIN MAX:", torch.min(latentTensor), torch.max(latentTensor), torch.mean(latentTensor))

            # Set progress bar info
            pbar.set_postfix({
                "feat_mod": f"{self._calcLatentModifier(intensity, tempo[i], spectroMean[i], spectroGrad[i], chromaSort):.2f}"
            })

            # Generate the image
            imgTensor = common_ksampler(
                    model,
                    seed,
                    steps,
                    cfg,
                    sampler_name,
                    scheduler,
                    promptPos,
                    promptNeg,
                    {"samples": latentTensor}, # ComfyUI, why package it?
                    denoise=denoise
                )[0]['samples']

            if outputTensor is None:
                outputTensor = imgTensor
            else:
                outputTensor = torch.vstack((
                    outputTensor,
                    # latentTensor,
                    imgTensor
                ))

            # Limit if one if supplied
            if (image_limit > 0) and (i >= (image_limit - 1)):
                break

            # Iterate seed as needed
            seed = self._iterateSeedByMode(seed, seed_mode)

            # Iterate the prompts as needed
            if (prompts.shape[0] != 1) and ((i + 1) < desiredFrames):
                promptPos = self._packPromptForComfy(promptSeqPos[i + 1])
                promptNeg = self._packPromptForComfy(promptSeqNeg[i + 1])

        # print(outputTensor)
        # print(outputTensor.shape)
        # for t in outputTensor:
        #     print(torch.min(t), torch.max(t), torch.mean(t))

        # import matplotlib.pyplot as plt
        # plt.figure(figsize=(10, 6))
        # plt.plot(tempo, label="Tempo")
        # # plt.plot(spectroMean, label="Spectro Mean")
        # plt.plot(spectroGrad, label="Spectro Grad")
        # # plt.plot(chromaSort, label="Chroma Sort")
        # plt.plot(np.array(modifiers), label="Modifiers")
        # plt.legend()
        # plt.show()

        return ({"samples": outputTensor}, fps)

    # Private Functions
    def _normalizeArray(self, array: np.ndarray, minVal: float = 0.0, maxVal: float = 1.0) -> np.ndarray:
        """
        Normalizes the given array between minVal and maxVal.

        array: A numpy array.
        minVal: The minimum value of the normalized array.
        maxVal: The maximum value of the normalized array.

        Returns a normalized numpy array.
        """
        arrayMin = np.min(array)
        return minVal + (array - arrayMin) * (maxVal - minVal) / (np.max(array) - arrayMin)

    def _iterateLatentByMode(self,
        latent: torch.Tensor,
        latentMode: str,
        modLimit: float,
        intensity: float,
        tempo: float,
        spectroMean: float,
        spectroGrad: float,
        chromaSort: float
    ) -> torch.Tensor:
        """
        Produces a latent tensor based on the provided mode.

        latent: The latent tensor to modify.
        latentMode: The mode to iterate by.
        modLimit: The maximum variation that can occur to the latent based on the latent's mean value. Provide `<= 0` to have no limit.
        intensity: The amount to modify the latent by each hop.
        tempo: The tempo of the audio.
        spectroMean: The normalized mean power of the audio.
        spectroGrad: The normalized power gradient of the audio.
        chromaSort: The sorted pitch chroma of the audio.

        Returns the iterated latent tensor.
        """
        # Decide what to do if in flow mode
        if latentMode == MusicVisualizer.LATENT_MODE_FLOW:
            # Each hop will add or subtract, based on the audio features, from the last latent
            if random.random() < 0.5:
                latentMode = MusicVisualizer.LATENT_MODE_INCREASE
            else:
                latentMode = MusicVisualizer.LATENT_MODE_DECREASE

        # Decide what to do based on mode
        if latentMode == MusicVisualizer.LATENT_MODE_INCREASE:
            # Each hop adds, based on the audio features, to the last latent
            return self._applyFeatToLatent(latent, MusicVisualizer.FEAT_APPLY_METHOD_ADD, modLimit, intensity, tempo, spectroMean, spectroGrad, chromaSort)
        elif latentMode == MusicVisualizer.LATENT_MODE_DECREASE:
            # Each hop subtracts, based on the audio features, from the last latent
            return self._applyFeatToLatent(latent, MusicVisualizer.FEAT_APPLY_METHOD_SUBTRACT, modLimit, intensity, tempo, spectroMean, spectroGrad, chromaSort)
        elif latentMode == MusicVisualizer.LATENT_MODE_GAUSS:
            # Each hop creates a new latent with guassian noise
            return self._createLatent(latent.shape)
        else: # LATENT_MODE_STATIC
            # Only the provided latent is used ignoring audio features
            return latent

    def _createLatent(self, size: tuple) -> torch.Tensor:
        """
        Creates a latent tensor from normal distribution noise.

        size: The size of the latent tensor.

        Returns the latent tensor.
        """
        # TODO: More specific noise range input
        return torch.tensor(np.random.normal(3, 2.5, size=size))
    
    def _calcLatentModifier(self,
        intensity: float,
        tempo: float,
        spectroMean: float,
        spectroGrad: float,
        chromaSort: float
    ) -> float:
        """
        Calculates the latent modifier based on the provided audio features.

        intensity: The amount to modify the latent by each hop.
        tempo: The tempo of the audio.
        spectroMean: The normalized mean power of the audio.
        spectroGrad: The normalized power gradient of the audio.
        chromaSort: The sorted pitch chroma of the audio.

        Returns the modifier.
        """
        # return (tempo * (spectroMean + spectroGrad)) + intensity # Is this a good equation? Who knows!
        return ((tempo + 1.0) * (spectroMean + 1.0)) * intensity # Normalize between -1.0 and 1.0 w/ spectro grad then multiply by tempo?

    def _applyFeatToLatent(self,
        latent: torch.Tensor,
        method: str,
        modLimit: float,
        intensity: float,
        tempo: float,
        spectroMean: float,
        spectroGrad: float,
        chromaSort: float
    ) -> torch.Tensor:
        """
        Applys the provided features to the latent tensor.

        latent: The latent tensor to modify.
        method: The method to use to apply the features.
        modLimit: The maximum variation that can occur to the latent based on the latent's mean value. Provide `<= 0` to have no limit.
        intensity: The amount to modify the latent by each hop.
        tempo: The tempo of the audio.
        spectroMean: The normalized mean power of the audio.
        spectroGrad: The normalized power gradient of the audio.
        chromaSort: The sorted pitch chroma of the audio.

        Returns the modified latent tensor.
        """
        # Calculate the modifier
        curMod = self._calcLatentModifier(intensity, tempo, spectroMean, spectroGrad, chromaSort)

        # Apply features to every point in the latent
        if method == MusicVisualizer.FEAT_APPLY_METHOD_ADD:
            # Add the features to the latent
            # Check if mean will be exceeded
            if (modLimit > 0) and (torch.mean(latent) + curMod) > modLimit:
                # Mean is exceeded so latent only
                return latent

            # Add the features to the latent
            latent += curMod
        else: # FEAT_APPLY_METHOD_SUBTRACT
            # Subtract the features from the latent
            # Check if mean will be exceeded
            if (modLimit > 0) and (torch.mean(latent) - curMod) < -modLimit:
                # Mean is exceeded so latent only
                return latent

            # Subtract the features from the latent
            latent -= curMod

        # TODO: wrap chromaSort over the whole thing

        return latent

    def _iterateSeedByMode(self, seed: int, seedMode: str):
        """
        Produces a seed based on the provided mode.

        seed: The seed to iterate.
        seedMode: The mode to iterate by.

        Returns the iterated seed.
        """
        if seedMode == MusicVisualizer.SEED_MODE_RANDOM:
            # Seed is random every hop
            return random.randint(0, 0xffffffffffffffff)
        elif seedMode == MusicVisualizer.SEED_MODE_INCREASE:
            # Seed increases by 1 every hop
            return seed + 1
        elif seedMode == MusicVisualizer.SEED_MODE_DECREASE:
            # Seed decreases by 1 every hop
            return seed - 1
        else: # SEED_MODE_FIXED
            # Seed stays the same
            return seed

    def _packPromptForComfy(self, prompt: torch.Tensor):
        """"
        Packages a prompt from the `PromptSequenceBuilder` node for use with ComfyUI's code.
        """
        return [[prompt.unsqueeze(0), {"pooled_output": None}]]

    @torch.jit.script
    def tensorLinspace(start: torch.Tensor, stop: torch.Tensor, num: int) -> torch.Tensor:
        """
        Replicates the multi-dimensional bahaviour of `numpy.linspace` in PyTorch.
        Function code and comments the functional solution provided in [Issue #61292 of PyTorch](https://github.com/pytorch/pytorch/issues/61292#issue-937937159).

        start: A Tensor of the same shape as `stop`.
        stop: A Tensor of the same shape as `start`.
        num: The number of steps to have.

        Returns a Tensor of shape `[num, *start.shape]` whose values are evenly spaced from start to end; inclusive.
        """
        # create a tensor of 'num' steps from 0 to 1
        steps = torch.arange(num, dtype=torch.float32, device=start.device) / (num - 1)

        # reshape the 'steps' tensor to [-1, *([1]*start.ndim)] to allow for broadcastings
        # - using 'steps.reshape([-1, *([1]*start.ndim)])' would be nice here but torchscript
        #   "cannot statically infer the expected size of a list in this contex", hence the code below
        for i in range(start.ndim):
            steps = steps.unsqueeze(-1)

        # the output starts at 'start' and increments until 'stop' in each dimension
        out = start[None] + steps*(stop - start)[None]

        return out
