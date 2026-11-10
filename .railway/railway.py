from railway_sdk import define_railway, github, project, service, volume


@define_railway
def main(ctx=None):
    thalamusVolume = volume("thalamus-volume", { "alerts": { "usage": { "100": {}, "80": {}, "95": {} } }, "allowOnlineResize": True, "region": "us-west2", "sizeMB": 500 })
    thalamus = service(
        "thalamus",
        source=github("akjcon/thalamus", **{ "branch": "main", "checkSuites": False }),
        build={ "buildEnvironment": "V3", "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
        deploy={ "restartPolicyType": "ALWAYS" },
        replicas={ "us-west2": 1 },
        volumeMounts={ "/data": thalamusVolume },
    )
    return project("thalamus", variables={"managed": False}, resources=[thalamus, thalamusVolume])
