ARG GO_IMAGE
ARG FRONTEND_IMAGE

FROM ${GO_IMAGE} AS build
ARG FAIL_PERCENT=30
WORKDIR /build
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.failPercent=${FAIL_PERCENT}" -o /wrapper .

FROM ${FRONTEND_IMAGE}
COPY --from=build /wrapper /src/wrapper
ENTRYPOINT ["/src/wrapper"]
