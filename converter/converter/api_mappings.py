"""
api_mappings.py -- Unity C# API to Roblox Luau mapping tables.

Comprehensive lookup tables used by the code transpiler to replace Unity API
calls, types, lifecycle hooks, and service imports with Roblox equivalents.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# API_CALL_MAP: Unity C# API -> Roblox Luau (100+ entries)
# ---------------------------------------------------------------------------

API_CALL_MAP: dict[str, str] = {
    # -- Debug --
    "Debug.Log": "print",
    "Debug.LogWarning": "warn",
    "Debug.LogError": "warn",
    # -- Physics --
    "Physics.Raycast": "workspace:Raycast",
    "Physics.RaycastAll": "workspace:Raycast",
    "Physics.OverlapSphere": "workspace:GetPartBoundsInRadius",
    "Physics.OverlapBox": "workspace:GetPartBoundsInBox",
    "Physics.SphereCast": "workspace:Spherecast",
    "Physics.Linecast": "workspace:Raycast",
    "Physics.gravity": "workspace.Gravity",
    # -- GameObject --
    "Instantiate": ".Clone",
    "Destroy": ".Destroy",
    "DestroyImmediate": ":Destroy()",
    "DontDestroyOnLoad": "-- DontDestroyOnLoad: parent to ReplicatedStorage",
    "SendMessage": ":SetAttribute",  # Approximate: use attributes for inter-component comms
    "BroadcastMessage": ":SetAttribute",  # Approximate
    "gameObject.SetActive(false)": "setActive(script.Parent, false)",
    "gameObject.SetActive(true)": "setActive(script.Parent, true)",
    ".activeSelf": ":GetAttribute('Active') ~= false",
    ".activeInHierarchy": ":GetAttribute('Active') ~= false",
    "gameObject.name": ".Name",
    "gameObject.tag": ":GetAttribute('Tag')",
    "gameObject.layer": "CollisionGroup",
    "CompareTag": ":GetAttribute('Tag') ==",
    "GameObject.Find": "workspace:FindFirstChild",
    "GameObject.FindWithTag": "CollectionService:GetTagged",
    "GameObject.FindGameObjectsWithTag": "CollectionService:GetTagged",
    # -- Transform --
    "transform.position": ".Position",
    "transform.localPosition": ".CFrame.Position",
    "transform.rotation": ".CFrame",
    "transform.localRotation": ".CFrame",
    "transform.eulerAngles": ".Orientation",
    "transform.localScale": ".Size",
    "transform.parent": ".Parent",
    "transform.SetParent": ".Parent =",
    "transform.Find": ":FindFirstChild",
    "transform.GetChild": ":GetChildren()",
    "transform.childCount": ":GetChildren()",
    "transform.forward": ".CFrame.LookVector",
    "transform.right": ".CFrame.RightVector",
    "transform.up": ".CFrame.UpVector",
    "transform.Translate": "CFrame.new",
    "transform.Rotate": "CFrame.Angles",
    "transform.LookAt": "CFrame.lookAt",
    "transform.TransformPoint": ":PointToWorldSpace",
    "transform.InverseTransformPoint": ":PointToObjectSpace",
    "transform.TransformDirection": ".CFrame:VectorToWorldSpace",
    "transform.InverseTransformDirection": ".CFrame:VectorToObjectSpace",
    "transform.TransformVector": ".CFrame:VectorToWorldSpace",
    "transform.localToWorldMatrix": ".CFrame",
    "transform.worldToLocalMatrix": ".CFrame:Inverse()",
    "transform.lossyScale": ".Size",
    "transform.root": ":FindFirstAncestorOfClass('Model') or script.Parent",
    # -- GetComponent --
    "GetComponent": ":FindFirstChildOfClass",
    "GetComponentInChildren": ":FindFirstChildOfClass",
    "GetComponentInParent": ":FindFirstAncestorOfClass",
    "GetComponents": ":GetChildren",
    "GetComponentsInChildren": ":GetDescendants",
    "AddComponent": "Instance.new",
    # -- Time --
    "Time.time": "workspace:GetServerTimeNow()",
    "Time.deltaTime": "dt",
    "Time.fixedDeltaTime": "dt",
    "Time.timeScale": "workspace:SetAttribute('TimeScale', 1)",
    "Time.unscaledDeltaTime": "dt",
    "Time.realtimeSinceStartup": "os.clock()",
    "Time.frameCount": "math.floor(tick() * 60)",  # Approximate frame counter
    # -- Input --
    "Input.GetKey": "UserInputService:IsKeyDown",
    "Input.GetKeyDown": "UserInputService.InputBegan",
    "Input.GetKeyUp": "UserInputService.InputEnded",
    "Input.GetButton": "UserInputService:IsKeyDown",
    "Input.GetButtonDown": "UserInputService.InputBegan",
    "Input.GetButtonUp": "UserInputService.InputEnded",
    "Input.GetMouseButton": "UserInputService:IsMouseButtonPressed",
    "Input.GetMouseButtonDown": "UserInputService.InputBegan",
    "Input.GetAxis": "-- Input.GetAxis",  # Handled by validator axis-specific mapping
    "Input.mousePosition": "UserInputService:GetMouseLocation()",
    "Input.GetTouch": "UserInputService.TouchStarted",
    "Input.anyKeyDown": "-- Input.anyKeyDown: use UserInputService.InputBegan",
    # -- Mathf --
    "Mathf.Abs": "math.abs",
    "Mathf.Ceil": "math.ceil",
    "Mathf.CeilToInt": "math.ceil",
    "Mathf.Clamp": "math.clamp",
    "Mathf.Clamp01": "math.clamp",
    "Mathf.Floor": "math.floor",
    "Mathf.FloorToInt": "math.floor",
    "Mathf.Lerp": "mathLerp",  # handled by UTILITY_FUNCTIONS
    "Mathf.Max": "math.max",
    "Mathf.Min": "math.min",
    "Mathf.Pow": "math.pow",
    "Mathf.Round": "math.round",
    "Mathf.Sqrt": "math.sqrt",
    "Mathf.Sin": "math.sin",
    "Mathf.Cos": "math.cos",
    "Mathf.Tan": "math.tan",
    "Mathf.Asin": "math.asin",
    "Mathf.Acos": "math.acos",
    "Mathf.Atan2": "math.atan2",
    "Mathf.Log": "math.log",
    "Mathf.Exp": "math.exp",
    "Mathf.Sign": "math.sign",
    "Mathf.PI": "math.pi",
    "Mathf.Infinity": "math.huge",
    "Mathf.Deg2Rad": "math.rad(1)",
    "Mathf.Rad2Deg": "math.deg(1)",
    "Mathf.MoveTowards": "mathMoveTowards",  # handled by UTILITY_FUNCTIONS
    "Mathf.SmoothDamp": "TweenService:Create",
    "Mathf.PingPong": "math.abs",
    "Mathf.PerlinNoise": "math.noise",
    # -- Vector3 --
    "Vector3.zero": "Vector3.zero",
    "Vector3.one": "Vector3.one",
    "Vector3.up": "Vector3.yAxis",
    "Vector3.down": "-Vector3.yAxis",
    "Vector3.forward": "Vector3.zAxis",
    "Vector3.back": "-Vector3.zAxis",
    "Vector3.right": "Vector3.xAxis",
    "Vector3.left": "-Vector3.xAxis",
    "Vector3.Distance": "(a - b).Magnitude",
    "Vector3.Lerp": ":Lerp",
    "Vector3.Normalize": ".Unit",
    "Vector3.Cross": ":Cross",
    "Vector3.Dot": ":Dot",
    "Vector3.Angle": "math.acos",
    "Vector3.MoveTowards": "vec3MoveTowards",  # handled by UTILITY_FUNCTIONS
    "Vector3.ClampMagnitude": ".Unit",
    "Vector3.SignedAngle": "vec3SignedAngle",  # handled by UTILITY_FUNCTIONS
    "Vector3.ProjectOnPlane": "vec3ProjectOnPlane",  # handled by UTILITY_FUNCTIONS
    "Vector3.Project": "vec3Project",  # handled by UTILITY_FUNCTIONS
    "Vector3.Reflect": "vec3Reflect",  # handled by UTILITY_FUNCTIONS
    "Vector3.SmoothDamp": ":Lerp",
    "new Vector3": "Vector3.new",
    # -- Vector2 --
    "Vector2.zero": "Vector2.zero",
    "Vector2.one": "Vector2.one",
    "Vector2.Distance": "(a - b).Magnitude",
    "new Vector2": "Vector2.new",
    # -- Quaternion --
    "Quaternion.identity": "CFrame.new()",
    "Quaternion.Euler": "CFrame.fromEulerAnglesXYZ",
    "Quaternion.LookRotation": "CFrame.lookAt",
    "Quaternion.Lerp": ":Lerp",
    "Quaternion.Slerp": ":Lerp",
    "Quaternion.Inverse": ":Inverse()",
    "Quaternion.AngleAxis": "CFrame.fromAxisAngle",
    "Quaternion.RotateTowards": ":Lerp",
    "Quaternion.Dot": "-- Quaternion.Dot: no direct CFrame equivalent",
    "Quaternion.FromToRotation": "-- FromToRotation: compute rotation between two directions",
    # -- Color --
    "Color.red": "Color3.new(1, 0, 0)",
    "Color.green": "Color3.new(0, 1, 0)",
    "Color.blue": "Color3.new(0, 0, 1)",
    "Color.white": "Color3.new(1, 1, 1)",
    "Color.black": "Color3.new(0, 0, 0)",
    "Color.yellow": "Color3.new(1, 1, 0)",
    "Color.cyan": "Color3.new(0, 1, 1)",
    "Color.magenta": "Color3.new(1, 0, 1)",
    "Color.gray": "Color3.new(0.5, 0.5, 0.5)",
    "new Color": "Color3.new",
    # -- Rigidbody --
    "rigidbody.velocity": ".AssemblyLinearVelocity",
    "rigidbody.angularVelocity": ".AssemblyAngularVelocity",
    "rigidbody.mass": ":GetMass()",
    "rigidbody.AddForce": ":ApplyImpulse",
    "rigidbody.AddTorque": ":ApplyAngularImpulse",
    "rigidbody.isKinematic": ".Anchored",
    "rigidbody.useGravity": "workspace.Gravity",
    "rigidbody.MovePosition": ".CFrame",
    # -- Collider events --
    "OnCollisionEnter": ".Touched",
    "OnCollisionExit": ".TouchEnded",
    "OnTriggerEnter": ".Touched",
    "OnTriggerExit": ".TouchEnded",
    # -- Coroutines --
    "StartCoroutine": "task.spawn",
    "StopCoroutine": "task.cancel",
    "StopAllCoroutines": "task.cancel",
    "yield return null": "task.wait()",
    "yield return new WaitForSeconds": "task.wait",
    "yield return new WaitForEndOfFrame": "task.wait()",
    "yield return new WaitForFixedUpdate": "task.wait()",
    # -- Scene --
    "SceneManager.LoadScene": "TeleportService:Teleport",
    "SceneManager.GetActiveScene": "game.PlaceId",
    # -- Audio --
    "AudioSource.Play": ":Play()",
    "AudioSource.Stop": ":Stop()",
    "AudioSource.Pause": ":Pause()",
    "AudioSource.PlayOneShot": ":Play()",
    "AudioSource.clip": ".SoundId",
    "AudioSource.volume": ".Volume",
    "AudioSource.pitch": ".PlaybackSpeed",
    "AudioSource.loop": ".Looped",
    "AudioSource.isPlaying": ".IsPlaying",
    # -- Animation --
    # Animator.StringToHash handled by validator (extracts string argument directly)
    "Animator.SetBool": ":SetAttribute",
    "Animator.SetFloat": ":SetAttribute",
    "Animator.SetInteger": ":SetAttribute",
    "Animator.GetBool": ":GetAttribute",
    "Animator.GetFloat": ":GetAttribute",
    "Animator.GetInteger": ":GetAttribute",
    "Animator.SetTrigger": "AnimationTrack:Play()",
    "Animator.ResetTrigger": "-- ResetTrigger: animation state reset",
    "Animator.Play": "AnimationTrack:Play()",
    "Animator.CrossFade": "AnimationTrack:Play()",
    "Animator.CrossFadeInFixedTime": "AnimationTrack:Play()",
    "Animation.Play": "AnimationTrack:Play()",
    # -- Camera --
    "Camera.main": "workspace.CurrentCamera",
    "Camera.fieldOfView": ".FieldOfView",
    "Camera.ScreenToWorldPoint": ":ScreenPointToRay",
    "Camera.WorldToScreenPoint": ":WorldToScreenPoint",
    # -- UI --
    "Canvas": "ScreenGui",
    "Text.text": ".Text",
    "Image.sprite": ".Image",
    "Button.onClick": ".Activated",
    "RectTransform": "UDim2",
    # -- PlayerPrefs --
    "PlayerPrefs.SetInt": "DataStoreService:GetDataStore('PlayerPrefs'):SetAsync",
    "PlayerPrefs.GetInt": "DataStoreService:GetDataStore('PlayerPrefs'):GetAsync",
    "PlayerPrefs.SetFloat": "DataStoreService:GetDataStore('PlayerPrefs'):SetAsync",
    "PlayerPrefs.GetFloat": "DataStoreService:GetDataStore('PlayerPrefs'):GetAsync",
    "PlayerPrefs.SetString": "DataStoreService:GetDataStore('PlayerPrefs'):SetAsync",
    "PlayerPrefs.GetString": "DataStoreService:GetDataStore('PlayerPrefs'):GetAsync",
    "PlayerPrefs.Save": "-- DataStoreService auto-saves",
    "PlayerPrefs.DeleteKey": "DataStoreService:GetDataStore('PlayerPrefs'):RemoveAsync",
    # -- Networking --
    "[Command]": "RemoteEvent:FireServer",
    "[ClientRpc]": "RemoteEvent:FireAllClients",
    "[SyncVar]": ":SetAttribute",
    # -- Random --
    "Random.Range": "math.random",
    "Random.value": "math.random()",
    "Random.insideUnitSphere": "Random.new():NextUnitVector()",
    "Random.insideUnitCircle": "Vector2.new(math.random() * 2 - 1, math.random() * 2 - 1).Unit",
    "UnityEngine.Random": "Random.new()",
    # -- String --
    "string.Format": "string.format",
    "string.IsNullOrEmpty": "(s == nil or s == '')",
    # -- Collections --
    # List<> and Dictionary<> handled by the variable declaration regex
    # (stripping generic types). Don't replace inline — it breaks lines.
    ".Clear()": "table.clear",
    "foreach": "for _, v in",
    # Queue
    ".Enqueue(": "table.insert(",
    ".Dequeue()": "table.remove(, 1)",  # post-processed by validator to fix syntax
    ".Peek()": "[1]",  # post-processed by validator
    # Stack
    ".Push(": "table.insert(",
    ".Pop()": "table.remove(, #)",  # post-processed by validator to fix syntax
    # LinkedList
    ".AddLast(": "table.insert(",
    ".AddFirst(": "table.insert(, 1, ",  # post-processed
    ".RemoveFirst()": "table.remove(, 1)",
    ".RemoveLast()": "table.remove(, #)",
    # -- TextMeshPro --
    "TextMeshProUGUI": "TextLabel",
    "TextMeshPro": "TextLabel",
    "TMP_Text": "TextLabel",
    "TMP_InputField": "TextBox",
    ".SetText(": ".Text =",
    # -- DOTween --
    "DOTween.To": "TweenService:Create",
    ".DOKill(": "-- DOKill: cancel active tween",
    ".SetEase(": "-- SetEase: use TweenInfo.new(duration, Enum.EasingStyle.Quad)",
    ".SetDelay(": "-- SetDelay: use task.delay(seconds, function()",
    ".SetLoops(": "-- SetLoops: use TweenInfo RepeatCount parameter",
    # -- Timeline / Playable Director --
    "PlayableDirector": "-- PlayableDirector: use animation scripts or TweenService sequences",
    "playableDirector.Play()": "-- PlayableDirector.Play: trigger animation sequence",
    "playableDirector.Stop()": "-- PlayableDirector.Stop: stop animation sequence",
    "playableDirector.Pause()": "-- PlayableDirector.Pause: pause animation sequence",
    "PlayableDirector.played": "-- PlayableDirector.played: use BindableEvent for sequence start",
    "PlayableDirector.stopped": "-- PlayableDirector.stopped: use BindableEvent for sequence end",
    ".OnComplete(": ".Completed:Connect(",
    # -- NavMesh --
    "NavMeshAgent": "-- NavMeshAgent: use Roblox PathfindingService",
    "NavMesh.CalculatePath": "PathfindingService:CreatePath()",
    ".SetDestination(": "-- SetDestination: use Path:ComputeAsync(target)",
    ".remainingDistance": "-- remainingDistance: compute from waypoints",
    ".isStopped": "-- isStopped: track manually",
    "navMeshAgent.speed": "-- NavMeshAgent.speed: set Humanoid.WalkSpeed",
    "agent.speed": "-- NavMeshAgent.speed: set Humanoid.WalkSpeed",
    "NavMeshObstacle": "-- NavMeshObstacle: no direct equivalent",
    # -- New Input System --
    "InputAction": "-- InputAction: use ContextActionService or UserInputService",
    ".ReadValue<Vector2>()": "-- ReadValue: use UserInputService input events",
    ".performed": "-- performed: use InputBegan/InputChanged",
    ".canceled": "-- canceled: use InputEnded",
    "PlayerInput": "-- PlayerInput: use UserInputService",
    # -- async/await --
    "async Task": "-- async Task: use task.spawn(function()",
    "async void": "-- async void: use task.spawn(function()",
    "await Task.Delay": "task.wait",
    "await Task.Yield()": "task.wait()",
    "UniTask": "-- UniTask: use task library",
    "await UniTask.Delay": "task.wait",
    "await UniTask.Yield()": "task.wait()",
    # -- LINQ --
    ".Where(": "linqWhere(",  # handled by UTILITY_FUNCTIONS
    ".Select(": "linqSelect(",  # handled by UTILITY_FUNCTIONS
    ".FirstOrDefault(": "linqFirstOrDefault(",  # handled by UTILITY_FUNCTIONS
    ".First(": "linqFirst(",  # handled by UTILITY_FUNCTIONS
    ".Any(": "linqAny(",  # handled by UTILITY_FUNCTIONS
    ".All(": "linqAll(",  # handled by UTILITY_FUNCTIONS
    ".OrderBy(": "linqOrderBy(",  # handled by UTILITY_FUNCTIONS
    ".OrderByDescending(": "linqOrderByDesc(",  # handled by UTILITY_FUNCTIONS
    ".ToList()": "",  # already a table in Luau
    ".ToArray()": "",  # already a table in Luau
    ".Sum(": "linqSum(",  # handled by UTILITY_FUNCTIONS
    ".Max(": "linqMax(",  # handled by UTILITY_FUNCTIONS
    ".Min(": "linqMin(",  # handled by UTILITY_FUNCTIONS
    ".Count(": "linqCount(",  # handled by UTILITY_FUNCTIONS
    ".Distinct()": "linqDistinct(",  # handled by UTILITY_FUNCTIONS
    ".GroupBy(": "linqGroupBy(",  # handled by UTILITY_FUNCTIONS
    ".SelectMany(": "linqSelectMany(",  # handled by UTILITY_FUNCTIONS
    ".Take(": "linqTake(",  # handled by UTILITY_FUNCTIONS
    ".Skip(": "linqSkip(",  # handled by UTILITY_FUNCTIONS
    ".Aggregate(": "linqAggregate(",  # handled by UTILITY_FUNCTIONS
    ".Last(": "linqLast(",  # handled by UTILITY_FUNCTIONS
    ".LastOrDefault(": "linqLastOrDefault(",  # handled by UTILITY_FUNCTIONS
    # -- Cinemachine --
    "CinemachineVirtualCamera": "-- CinemachineVirtualCamera: configure workspace.CurrentCamera",
    "CinemachineBrain": "-- CinemachineBrain: use workspace.CurrentCamera",
    ".m_Lens.FieldOfView": ".FieldOfView",
    # -- Terrain --
    "Terrain.activeTerrain": "-- Terrain: use Roblox Terrain object",
    "TerrainData": "-- TerrainData: use workspace.Terrain",
    ".GetHeight(": "-- GetHeight: no direct equivalent",
    "SplatPrototype": "-- SplatPrototype: use Terrain:FillBall/FillBlock with materials",
    # -- 2D Physics --
    "Rigidbody2D": "-- Rigidbody2D: no 2D physics in Roblox",
    "Physics2D.Raycast": "workspace:Raycast",
    "Collider2D": "BasePart",
    # -- Misc --
    "Application.targetFrameRate": "-- targetFrameRate: no equivalent",
    "Application.persistentDataPath": "-- persistentDataPath: use DataStoreService",
    "Addressables.LoadAssetAsync": "-- Addressables: use ReplicatedStorage:WaitForChild",
    "Object.FindObjectOfType": "-- FindObjectOfType: use workspace:FindFirstChildOfClass",
    "Object.FindObjectsOfType": "-- FindObjectsOfType: use workspace:GetDescendants()",
    "LayerMask": "-- LayerMask: use CollisionGroups",
    "Physics.IgnoreCollision": "-- IgnoreCollision: use CollisionGroups",
    "WaitUntil": "-- WaitUntil: use while loop with task.wait()",
    "WaitWhile": "-- WaitWhile: use while loop with task.wait()",
    "Invoke(\"": "task.delay(",  # MonoBehaviour.Invoke("method", delay)
    # InvokeRepeating handled by validator (needs arg parsing)
    "CancelInvoke": "-- CancelInvoke: cancel spawned task",
    # -- UnityEvent --
    ".AddListener(": ".Event:Connect(",
    ".RemoveListener(": "-- RemoveListener: disconnect the connection",
    ".RemoveAllListeners()": "-- RemoveAllListeners: disconnect all connections",
    # -- System.Action / delegates --
    "Action<": "-- Action: use function type",
    # Func<> handled by validator generic type stripping
    # -- Events / Delegates --
    # Note: "event Action<" is handled by the sanitizer, not here
    # to avoid conflicts with the "Action<" mapping
    # -- Mathf additional --
    "Mathf.Repeat": "mathRepeat",  # handled by UTILITY_FUNCTIONS
    "Mathf.DeltaAngle": "mathDeltaAngle",  # handled by UTILITY_FUNCTIONS
    "Mathf.LerpAngle": "mathLerpAngle",  # handled by UTILITY_FUNCTIONS
    "Mathf.InverseLerp": "mathInverseLerp",  # handled by UTILITY_FUNCTIONS
    "Mathf.SmoothStep": "mathSmoothStep",  # handled by UTILITY_FUNCTIONS
    "Mathf.Approximately": "mathApproximately",  # handled by UTILITY_FUNCTIONS
    "Mathf.NegativeInfinity": "-math.huge",
    "Mathf.Epsilon": "1e-7",
    "Mathf.NextPowerOfTwo": "-- NextPowerOfTwo: use bit32 operations",
    # -- Rigidbody additional --
    "rigidbody.drag": "-- drag: set CustomPhysicalProperties",
    "rigidbody.constraints": "-- constraints: set Anchored or use constraints",
    "rigidbody.Sleep": "-- Sleep: no Roblox equivalent",
    "rigidbody.WakeUp": "-- WakeUp: no Roblox equivalent",
    "rigidbody.IsSleeping": "-- IsSleeping: no Roblox equivalent",
    # -- Physics additional --
    "Physics.CheckSphere": "workspace:GetPartBoundsInRadius",
    "Physics.CheckBox": "workspace:GetPartBoundsInBox",
    "Physics.defaultContactOffset": "-- defaultContactOffset: no equivalent",
    # -- String additional --
    "string.Replace": "string.gsub",
    "string.Contains": "string.find",
    "string.StartsWith": "string.sub",
    "string.EndsWith": "string.sub",
    "string.Split": "string.split",
    "string.Trim": "string.match",
    "string.ToLower": "string.lower",
    "string.ToUpper": "string.upper",
    "string.Substring": "string.sub",
    "string.Join": "table.concat",
    "string.Concat": "..",
    "int.Parse": "tonumber",
    "float.Parse": "tonumber",
    "double.Parse": "tonumber",
    "bool.Parse": "-- bool.Parse: use value == 'true'",
    ".Length": "#",  # array/string length
    # -- Math additional --
    "Math.Abs": "math.abs",
    "Math.Max": "math.max",
    "Math.Min": "math.min",
    "Math.Floor": "math.floor",
    "Math.Ceil": "math.ceil",
    "Math.Round": "math.round",
    "Math.Sqrt": "math.sqrt",
    "Math.PI": "math.pi",
    # -- Array/List --
    # .Add/.Remove/.RemoveAt/.Insert/.IndexOf handled by luau_validator regex
    # (simple string replacement can't restructure obj.Method(arg) → func(obj, arg))
    ".Reverse()": "-- Reverse: reverse table in-place",
    ".Sort()": "table.sort",
    # ContainsKey and TryGetValue handled by luau_validator, not here
    # (inline comment replacement would break the code line)
    ".GetInstanceID()": "-- GetInstanceID: no Roblox equivalent",
    ".Equals(": "==",
    ".GetHashCode()": "-- GetHashCode: no equivalent",
    ".GetType()": "typeof",
    # -- Cursor --
    "Cursor.lockState": "UserInputService.MouseBehavior",
    "Cursor.visible": "UserInputService.MouseIconEnabled",
    "CursorLockMode.Locked": "Enum.MouseBehavior.LockCenter",
    "CursorLockMode.None": "Enum.MouseBehavior.Default",
    "CursorLockMode.Confined": "Enum.MouseBehavior.LockCurrentPosition",
    # -- Application --
    "Application.isPlaying": "game:GetService('RunService'):IsRunning()",
    "Application.platform": "-- Application.platform: use game:GetService('UserInputService').TouchEnabled",
    "Application.isFocused": "game:GetService('UserInputService').WindowFocused",
    "Application.isEditor": "game:GetService('RunService'):IsStudio()",
    "Application.Quit()": "-- Application.Quit: use game.Players.LocalPlayer:Kick()",
    # -- Screen --
    "Screen.width": "workspace.CurrentCamera.ViewportSize.X",
    "Screen.height": "workspace.CurrentCamera.ViewportSize.Y",
    # -- Resources --
    "Resources.Load": "-- Resources.Load: use game.ReplicatedStorage:FindFirstChild",
    # -- SceneManager --
    "SceneManager.LoadScene": "game:GetService('TeleportService'):Teleport",
    "SceneManager.LoadSceneAsync": "game:GetService('TeleportService'):TeleportAsync",
    "SceneManager.GetActiveScene": "-- GetActiveScene: use game.PlaceId",
    # -- PlayerPrefs --
    "PlayerPrefs.GetFloat": "-- PlayerPrefs: use DataStoreService or Player:GetAttribute",
    "PlayerPrefs.SetFloat": "-- PlayerPrefs: use DataStoreService or Player:SetAttribute",
    "PlayerPrefs.GetInt": "-- PlayerPrefs: use DataStoreService",
    "PlayerPrefs.SetInt": "-- PlayerPrefs: use DataStoreService",
    "PlayerPrefs.GetString": "-- PlayerPrefs: use DataStoreService",
    "PlayerPrefs.SetString": "-- PlayerPrefs: use DataStoreService",
    "PlayerPrefs.Save": "-- PlayerPrefs.Save: DataStore saves automatically",
    "PlayerPrefs.DeleteAll": "-- PlayerPrefs.DeleteAll: clear DataStore",
    # -- CharacterController --
    ".isGrounded": "-- isGrounded: use Humanoid:GetState() == Enum.HumanoidStateType.Running",
    "CharacterController.Move": "Humanoid:Move",
    "CharacterController.SimpleMove": "Humanoid:Move",
    # -- Physics cast additional --
    "Physics.SphereCastAll": "workspace:Spherecast",
    "Physics.BoxCast": "workspace:Blockcast",
    "Physics.BoxCastAll": "workspace:Blockcast",
    "Physics.CapsuleCast": "workspace:Spherecast",
    # -- Renderer --
    ".material.color": ".Color",
    ".material.mainTexture": "-- mainTexture: use SurfaceAppearance",
    ".sharedMaterial": "-- sharedMaterial: use part properties",
    ".materials": "-- materials: use SurfaceAppearance",
    ".enabled": ".Visible",
    # -- Misc additional --
    "FindObjectOfType": "workspace:FindFirstChildOfClass",
    "FindObjectsOfType": "workspace:GetDescendants()",
    "GameObject.Instantiate": ":Clone()",
    "Invoke(": "task.delay(",
    # -- Destroy with delay --
    "Destroy(gameObject,": "Debris:AddItem(script.Parent,",
    "Destroy(this.gameObject,": "Debris:AddItem(script.Parent,",
    # -- Array/Collection operations --
    "Array.Resize": "-- Array.Resize: tables resize automatically in Luau",
    "Array.Copy": "table.move",
    "Array.IndexOf": "table.find",
    "Array.Sort": "table.sort",
    "Array.Clear": "table.clear",
    "Array.Reverse": "-- Array.Reverse: use for loop to reverse table",
    # -- String path operations --
    "Path.Combine": "-- Path.Combine: use .. to concatenate paths",
    "Path.GetFileName": "string.match",
    "Path.GetExtension": "string.match",
    "Path.GetDirectoryName": "string.match",
    # -- ExecuteEvents (Unity UI event system) --
    "ExecuteEvents.Execute": "-- ExecuteEvents: use Roblox event system",
    "ExecuteEvents.ExecuteHierarchy": "-- ExecuteEvents: use Roblox event system",
    # -- LayoutRebuilder --
    "LayoutRebuilder.MarkLayoutForRebuild": "-- LayoutRebuilder: UI auto-sizes in Roblox",
    "LayoutRebuilder.ForceRebuildLayoutImmediate": "-- LayoutRebuilder: UI auto-sizes in Roblox",
    # -- EditorGUILayout / GUI (strip editor-only code) --
    "EditorGUI.": "-- EditorGUI: editor-only",
    "EditorGUILayout.": "-- EditorGUILayout: editor-only",
    "GUILayout.": "-- GUILayout: editor-only",
    "GUI.Button(": "-- GUI.Button: editor-only",
    # -- Shader --
    "Shader.PropertyToID": "-- Shader.PropertyToID: no equivalent",
    "Shader.Find": "-- Shader.Find: no equivalent",
}


# ---------------------------------------------------------------------------
# TYPE_MAP: C# types -> Luau types (40+ entries)
# ---------------------------------------------------------------------------

TYPE_MAP: dict[str, str] = {
    # Numeric
    "int": "number",
    "float": "number",
    "double": "number",
    "long": "number",
    "short": "number",
    "byte": "number",
    "uint": "number",
    "ulong": "number",
    "ushort": "number",
    "sbyte": "number",
    "decimal": "number",
    # Boolean
    "bool": "boolean",
    "Boolean": "boolean",
    # String
    "string": "string",
    "String": "string",
    "char": "string",
    # Void
    "void": "()",
    # Inferred
    "var": "",
    "dynamic": "any",
    "object": "any",
    # Unity math types
    "Vector3": "Vector3",
    "Vector2": "Vector2",
    "Vector4": "Vector3",  # no Vector4 in Roblox; approximate
    "Quaternion": "CFrame",
    "Matrix4x4": "CFrame",
    # Unity color
    "Color": "Color3",
    "Color32": "Color3",
    # Unity core
    "Transform": "BasePart",
    "GameObject": "Instance",
    "Rigidbody": "BasePart",
    "Rigidbody2D": "BasePart",
    "Collider": "BasePart",
    "Collider2D": "BasePart",
    "BoxCollider": "BasePart",
    "SphereCollider": "BasePart",
    "CapsuleCollider": "BasePart",
    "MeshCollider": "BasePart",
    "CharacterController": "Humanoid",
    # Audio
    "AudioSource": "Sound",
    "AudioClip": "Sound",
    # Camera
    "Camera": "Camera",
    # Animation
    "Animator": "AnimationController",
    "AnimationClip": "Animation",
    "RuntimeAnimatorController": "AnimationController",
    # MonoBehaviour base
    "MonoBehaviour": "-- MonoBehaviour: Roblox script",
    "ScriptableObject": "-- ScriptableObject: use ModuleScript table",
    # Physics
    "RaycastHit": "RaycastResult",
    "Ray": "Ray",
    "Bounds": "Region3",
    "Rect": "UDim2",
    # UI types
    "Canvas": "ScreenGui",
    "RectTransform": "Frame",
    "Text": "TextLabel",
    "Image": "ImageLabel",
    "Button": "TextButton",
    "Slider": "Frame",
    "Toggle": "TextButton",
    "InputField": "TextBox",
    "Dropdown": "Frame",
    "ScrollRect": "ScrollingFrame",
    "RawImage": "ImageLabel",
    # TextMeshPro
    "TextMeshProUGUI": "TextLabel",
    "TextMeshPro": "TextLabel",
    "TMP_Text": "TextLabel",
    "TMP_InputField": "TextBox",
    # Navigation
    "NavMeshAgent": "PathfindingService",
    # Networking
    "NetworkBehaviour": "-- NetworkBehaviour: use RemoteEvents",
    # Collections (generic)
    "List": "{}",
    "Dictionary": "{}",
    "HashSet": "{}",
    "Queue": "{}",
    "Stack": "{}",
    "Array": "{}",
    # Coroutine
    "IEnumerator": "() -> ()",
    "Coroutine": "thread",
    # Task
    "Task": "thread",
    "UniTask": "thread",
    # Events
    "UnityEvent": "BindableEvent",
    "UnityAction": "() -> ()",
    # Misc
    "LayerMask": "number",
    "Sprite": "string",  # rbxassetid URL
    "Texture2D": "string",
    "Material": "string",
    "Shader": "string",
    "Mesh": "string",
    "PhysicMaterial": "string",
    "WaitForSeconds": "number",
    "WaitForEndOfFrame": "number",
    "Object": "Instance",
    "Component": "Instance",
    "Behaviour": "Instance",
    "MonoBehaviour[]": "{}",
    "Light": "PointLight",
    "ParticleSystem": "ParticleEmitter",
    "Terrain": "Terrain",
    "LineRenderer": "Beam",
    "TrailRenderer": "Trail",
    "Cloth": "Instance",
    "Joint": "Constraint",
    "RectTransform": "Frame",
    "Canvas": "ScreenGui",
    "Animator": "AnimationController",
    "Animation": "AnimationController",
    "AudioClip": "Sound",
    "AnimationCurve": "NumberSequence",
    "Gradient": "ColorSequence",
    "ScriptableObject": "ModuleScript",
    "TextAsset": "string",
    "NavMeshAgent": "Instance",
    "GUIStyle": "Instance",
}


# ---------------------------------------------------------------------------
# LIFECYCLE_MAP: Unity lifecycle hooks -> Roblox equivalents (15+ entries)
# ---------------------------------------------------------------------------

LIFECYCLE_MAP: dict[str, str] = {
    "Awake": "-- Awake: runs at top of script (module initialization)",
    "Start": "-- Start: runs after all Awake calls (use task.defer or Players.PlayerAdded)",
    "Update": "RunService.Heartbeat:Connect(function(dt)",
    "FixedUpdate": "RunService.Heartbeat:Connect(function(dt)",
    "LateUpdate": "RunService.RenderStepped:Connect(function(dt)",
    "OnEnable": "-- OnEnable: connect events here",
    "OnDisable": "-- OnDisable: disconnect events here",
    "OnDestroy": "-- OnDestroy: use Instance.Destroying or Maid pattern",
    "OnCollisionEnter": "part.Touched:Connect(function(otherPart)",
    "OnCollisionExit": "part.TouchEnded:Connect(function(otherPart)",
    "OnTriggerEnter": "part.Touched:Connect(function(otherPart)",
    "OnTriggerExit": "part.TouchEnded:Connect(function(otherPart)",
    "OnMouseDown": "ClickDetector.MouseClick:Connect(function(player)",
    "OnMouseEnter": "ClickDetector.MouseHoverEnter:Connect(function(player)",
    "OnMouseExit": "ClickDetector.MouseHoverLeave:Connect(function(player)",
    "OnGUI": "-- OnGUI: use ScreenGui with Frames/TextLabels",
    "OnApplicationQuit": "game:BindToClose(function()",
    "OnApplicationPause": "-- OnApplicationPause: no direct equivalent",
}


# ---------------------------------------------------------------------------
# SERVICE_IMPORTS: Roblox service import statements (18+ entries)
# ---------------------------------------------------------------------------

SERVICE_IMPORTS: dict[str, str] = {
    "RunService": 'local RunService = game:GetService("RunService")',
    "UserInputService": 'local UserInputService = game:GetService("UserInputService")',
    "TweenService": 'local TweenService = game:GetService("TweenService")',
    "CollectionService": 'local CollectionService = game:GetService("CollectionService")',
    "Players": 'local Players = game:GetService("Players")',
    "ReplicatedStorage": 'local ReplicatedStorage = game:GetService("ReplicatedStorage")',
    "ServerStorage": 'local ServerStorage = game:GetService("ServerStorage")',
    "DataStoreService": 'local DataStoreService = game:GetService("DataStoreService")',
    "TeleportService": 'local TeleportService = game:GetService("TeleportService")',
    "SoundService": 'local SoundService = game:GetService("SoundService")',
    "Workspace": 'local Workspace = game:GetService("Workspace")',
    "PathfindingService": 'local PathfindingService = game:GetService("PathfindingService")',
    "ContextActionService": 'local ContextActionService = game:GetService("ContextActionService")',
    "GuiService": 'local GuiService = game:GetService("GuiService")',
    "MarketplaceService": 'local MarketplaceService = game:GetService("MarketplaceService")',
    "PhysicsService": 'local PhysicsService = game:GetService("PhysicsService")',
    "HttpService": 'local HttpService = game:GetService("HttpService")',
    "TextService": 'local TextService = game:GetService("TextService")',
    "Debris": 'local Debris = game:GetService("Debris")',
    "ContentProvider": 'local ContentProvider = game:GetService("ContentProvider")',
}


# ---------------------------------------------------------------------------
# UTILITY_FUNCTIONS: Luau helpers injected when certain Mathf mappings are used
# ---------------------------------------------------------------------------

UTILITY_FUNCTIONS: dict[str, str] = {
    "mathLerp": """\
local function mathLerp(a, b, t)
\treturn a + (b - a) * math.clamp(t, 0, 1)
end""",
    "mathRepeat": """\
local function mathRepeat(t, length)
\treturn t - math.floor(t / length) * length
end""",
    "mathDeltaAngle": """\
local function mathDeltaAngle(current, target)
\tlocal d = mathRepeat(target - current, 360)
\tif d > 180 then d = d - 360 end
\treturn d
end""",
    "mathLerpAngle": """\
local function mathLerpAngle(a, b, t)
\tlocal d = mathDeltaAngle(a, b)
\treturn a + d * math.clamp(t, 0, 1)
end""",
    "mathInverseLerp": """\
local function mathInverseLerp(a, b, value)
\tif a ~= b then return math.clamp((value - a) / (b - a), 0, 1) end
\treturn 0
end""",
    "mathSmoothStep": """\
local function mathSmoothStep(from, to, t)
\tt = math.clamp(t, 0, 1)
\tt = t * t * (3 - 2 * t)
\treturn from + (to - from) * t
end""",
    "mathApproximately": """\
local function mathApproximately(a, b)
\treturn math.abs(a - b) < 1e-6
end""",
    "mathMoveTowards": """\
local function mathMoveTowards(current, target, maxDelta)
\tif math.abs(target - current) <= maxDelta then return target end
\treturn current + math.sign(target - current) * maxDelta
end""",
    "vec3MoveTowards": """\
local function vec3MoveTowards(current, target, maxDistanceDelta)
\tlocal diff = target - current
\tlocal dist = diff.Magnitude
\tif dist <= maxDistanceDelta or dist < 1e-6 then return target end
\treturn current + diff / dist * maxDistanceDelta
end""",
    # LINQ utility functions
    "linqWhere": """\
local function linqWhere(tbl, predicate)
\tlocal result = {}
\tfor _, v in tbl do
\t\tif predicate(v) then table.insert(result, v) end
\tend
\treturn result
end""",
    "linqSelect": """\
local function linqSelect(tbl, selector)
\tlocal result = {}
\tfor _, v in tbl do
\t\ttable.insert(result, selector(v))
\tend
\treturn result
end""",
    "linqFirstOrDefault": """\
local function linqFirstOrDefault(tbl, predicate)
\tif not predicate then return tbl[1] end
\tfor _, v in tbl do
\t\tif predicate(v) then return v end
\tend
\treturn nil
end""",
    "linqFirst": """\
local function linqFirst(tbl, predicate)
\tif not predicate then return tbl[1] end
\tfor _, v in tbl do
\t\tif predicate(v) then return v end
\tend
\terror("Sequence contains no matching element")
end""",
    "linqAny": """\
local function linqAny(tbl, predicate)
\tif not predicate then return #tbl > 0 end
\tfor _, v in tbl do
\t\tif predicate(v) then return true end
\tend
\treturn false
end""",
    "linqAll": """\
local function linqAll(tbl, predicate)
\tfor _, v in tbl do
\t\tif not predicate(v) then return false end
\tend
\treturn true
end""",
    "linqOrderBy": """\
local function linqOrderBy(tbl, keySelector)
\tlocal copy = table.clone(tbl)
\ttable.sort(copy, function(a, b) return keySelector(a) < keySelector(b) end)
\treturn copy
end""",
    "linqOrderByDesc": """\
local function linqOrderByDesc(tbl, keySelector)
\tlocal copy = table.clone(tbl)
\ttable.sort(copy, function(a, b) return keySelector(a) > keySelector(b) end)
\treturn copy
end""",
    "linqSum": """\
local function linqSum(tbl, selector)
\tlocal total = 0
\tfor _, v in tbl do
\t\ttotal = total + (if selector then selector(v) else v)
\tend
\treturn total
end""",
    "linqMax": """\
local function linqMax(tbl, selector)
\tlocal best = -math.huge
\tfor _, v in tbl do
\t\tlocal val = if selector then selector(v) else v
\t\tif val > best then best = val end
\tend
\treturn best
end""",
    "linqMin": """\
local function linqMin(tbl, selector)
\tlocal best = math.huge
\tfor _, v in tbl do
\t\tlocal val = if selector then selector(v) else v
\t\tif val < best then best = val end
\tend
\treturn best
end""",
    "linqCount": """\
local function linqCount(tbl, predicate)
\tif not predicate then return #tbl end
\tlocal count = 0
\tfor _, v in tbl do
\t\tif predicate(v) then count = count + 1 end
\tend
\treturn count
end""",
    "linqDistinct": """\
local function linqDistinct(tbl)
\tlocal seen = {}
\tlocal result = {}
\tfor _, v in tbl do
\t\tif not seen[v] then
\t\t\tseen[v] = true
\t\t\ttable.insert(result, v)
\t\tend
\tend
\treturn result
end""",
    "linqGroupBy": """\
local function linqGroupBy(tbl, keySelector)
\tlocal groups = {}
\tlocal order = {}
\tfor _, v in tbl do
\t\tlocal key = keySelector(v)
\t\tif not groups[key] then
\t\t\tgroups[key] = {}
\t\t\ttable.insert(order, key)
\t\tend
\t\ttable.insert(groups[key], v)
\tend
\tlocal result = {}
\tfor _, key in order do
\t\ttable.insert(result, {Key = key, Values = groups[key]})
\tend
\treturn result
end""",
    "linqSelectMany": """\
local function linqSelectMany(tbl, selector)
\tlocal result = {}
\tfor _, v in tbl do
\t\tlocal inner = selector(v)
\t\tfor _, item in inner do
\t\t\ttable.insert(result, item)
\t\tend
\tend
\treturn result
end""",
    "linqTake": """\
local function linqTake(tbl, count)
\tlocal result = {}
\tfor i = 1, math.min(count, #tbl) do
\t\ttable.insert(result, tbl[i])
\tend
\treturn result
end""",
    "linqSkip": """\
local function linqSkip(tbl, count)
\tlocal result = {}
\tfor i = count + 1, #tbl do
\t\ttable.insert(result, tbl[i])
\tend
\treturn result
end""",
    "linqAggregate": """\
local function linqAggregate(tbl, seed, func)
\tlocal acc = seed
\tfor _, v in tbl do
\t\tacc = func(acc, v)
\tend
\treturn acc
end""",
    "linqLast": """\
local function linqLast(tbl, predicate)
\tif not predicate then return tbl[#tbl] end
\tfor i = #tbl, 1, -1 do
\t\tif predicate(tbl[i]) then return tbl[i] end
\tend
\terror("Sequence contains no matching element")
end""",
    "linqLastOrDefault": """\
local function linqLastOrDefault(tbl, predicate)
\tif not predicate then return tbl[#tbl] end
\tfor i = #tbl, 1, -1 do
\t\tif predicate(tbl[i]) then return tbl[i] end
\tend
\treturn nil
end""",
    # Vector3 utility functions
    "vec3SignedAngle": """\
local function vec3SignedAngle(from, to, axis)
\tlocal cross = from:Cross(to)
\tlocal angle = math.atan2(cross.Magnitude, from:Dot(to))
\tif cross:Dot(axis) < 0 then angle = -angle end
\treturn math.deg(angle)
end""",
    "vec3ProjectOnPlane": """\
local function vec3ProjectOnPlane(vector, planeNormal)
\treturn vector - planeNormal * vector:Dot(planeNormal)
end""",
    "vec3Project": """\
local function vec3Project(vector, onNormal)
\tlocal sqrMag = onNormal:Dot(onNormal)
\tif sqrMag < 1e-15 then return Vector3.zero end
\treturn onNormal * (vector:Dot(onNormal) / sqrMag)
end""",
    "vec3Reflect": """\
local function vec3Reflect(direction, normal)
\treturn direction - 2 * direction:Dot(normal) * normal
end""",
    "setActive": """\
local function setActive(instance, active)
\tif not instance then return end
\tif instance:IsA("BasePart") then
\t\tinstance.Transparency = active and 0 or 1
\t\tinstance.CanCollide = active
\tend
\tfor _, child in instance:GetDescendants() do
\t\tif child:IsA("BasePart") then
\t\t\tchild.Transparency = active and 0 or 1
\t\t\tchild.CanCollide = active
\t\telseif child:IsA("ParticleEmitter") or child:IsA("Light") or child:IsA("BillboardGui") or child:IsA("SurfaceGui") then
\t\t\tchild.Enabled = active
\t\telseif child:IsA("Sound") then
\t\t\tif not active and child.Playing then child:Stop() end
\t\tend
\tend
end""",
}
