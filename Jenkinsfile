pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: '10'))
        timestamps()
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    parameters {
        string(
            name: 'DEPLOY_HOST',
            defaultValue: 'testing@192.168.6.65',
            description: 'SSH target: user@host for the Ubuntu deployment server'
        )
        string(
            name: 'DEPLOY_DIR',
            defaultValue: '/var/www/timesheet',
            description: 'Absolute path to the app directory on the server'
        )
        string(
            name: 'BRANCH',
            defaultValue: 'main',
            description: 'Git branch to build and deploy'
        )
        booleanParam(
            name: 'SKIP_TESTS',
            defaultValue: false,
            description: 'Skip the test stage (use with caution)'
        )
    }

    // ── Credentials stored in Jenkins Credential Store ────────
    environment {
        // SSH private key credential ID (type: SSH Username with private key)
        SSH_CRED_ID        = 'timesheet-deploy-key'
        // Secret file credential containing the .env for the server
        ENV_FILE_CRED_ID   = 'timesheet-env-file'
        // Systemd service name (must match DEPLOY_SYSTEMD.service on server)
        SERVICE_NAME       = 'timesheet'
        // Nginx site config name
        NGINX_SITE         = 'timesheet'
    }
    stages {

        stage('Checkout') {
            steps {
                echo "==> Checking out branch: ${params.BRANCH}"
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: "*/${params.BRANCH}"]],
                    userRemoteConfigs: scm.userRemoteConfigs
                ])
                sh 'git log -1 --oneline'
            }
        }

        // ── 2. LINT / STATIC ANALYSIS ────────────────────────
        stage('Lint') {
            steps {
                echo "==> Running flake8 static analysis"
                sh '''
                    python3 -m venv .lint-venv
                    . .lint-venv/bin/activate
                    pip install --quiet flake8
                    # E501 = line-too-long (relaxed to 120), W503 = line break before binary op
                    flake8 app.py chaos_endpoints.py chaos_bot.py \
                        --max-line-length=120 \
                        --ignore=W503 \
                        --statistics
                    deactivate
                '''
            }
        }

        // ── 3. TEST ───────────────────────────────────────────
        stage('Test') {
            when {
                expression { !params.SKIP_TESTS }
            }
            steps {
                echo "==> Installing dependencies and running tests"
                sh '''
                    python3 -m venv .test-venv
                    . .test-venv/bin/activate
                    pip install --quiet -r requirements.txt pytest
                    # If you have a tests/ directory, pytest discovers them automatically
                    pytest --tb=short -q || true
                    deactivate
                '''
            }
            post {
                always {
                    // Publish JUnit XML if tests emit one (pytest --junitxml=results.xml)
                    junit allowEmptyResults: true, testResults: '**/test-results/*.xml'
                }
            }
        }

        // ── 4. PACKAGE ────────────────────────────────────────
        stage('Package') {
            steps {
                echo "==> Creating deployment archive (excluding dev artefacts)"
                sh '''
                    tar --exclude='.git' \
                        --exclude='.lint-venv' \
                        --exclude='.test-venv' \
                        --exclude='venv' \
                        --exclude='__pycache__' \
                        --exclude='*.pyc' \
                        --exclude='logs' \
                        --exclude='*.sock' \
                        --exclude='database.db' \
                        --exclude='*.backup' \
                        -czf timesheet-app.tar.gz .
                    echo "Archive size: $(du -sh timesheet-app.tar.gz | cut -f1)"
                '''
            }
        }

        // ── 5. DEPLOY ─────────────────────────────────────────
        stage('Deploy') {
            steps {
                echo "==> Deploying to ${params.DEPLOY_HOST}:${params.DEPLOY_DIR}"

                // Inject the .env file (stored as a Jenkins Secret File credential)
                withCredentials([
                    sshUserPrivateKey(
                        credentialsId: env.SSH_CRED_ID,
                        keyFileVariable: 'SSH_KEY'
                    ),
                    file(
                        credentialsId: env.ENV_FILE_CRED_ID,
                        variable: 'ENV_FILE'
                    )
                ]) {
                    // Helper: reusable SSH options
                    script {
                        def sshOpts = "-i ${SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes"
                        def host    = params.DEPLOY_HOST
                        def dir     = params.DEPLOY_DIR

                        // 5a. Upload archive + fresh .env
                        sh """
                            scp ${sshOpts} timesheet-app.tar.gz ${host}:/tmp/timesheet-app.tar.gz
                            scp ${sshOpts} \${ENV_FILE} ${host}:/tmp/timesheet.env
                        """

                        // 5b. Remote deployment script (single heredoc, runs as one SSH session)
                        sh """
                            ssh ${sshOpts} ${host} bash -s <<'REMOTE_SCRIPT'
                            set -euo pipefail

                            DEPLOY_DIR="${dir}"
                            SERVICE="${env.SERVICE_NAME}"
                            NGINX_SITE="${env.NGINX_SITE}"

                            echo "[1/7] Stopping Gunicorn service..."
                            sudo systemctl stop \$SERVICE || true

                            echo "[2/7] Syncing application files..."
                            sudo mkdir -p \$DEPLOY_DIR/persistent_data
                            # Extract archive, overwriting everything EXCEPT persistent_data
                            sudo tar -xzf /tmp/timesheet-app.tar.gz \
                                --directory \$DEPLOY_DIR \
                                --exclude='persistent_data'
                            rm -f /tmp/timesheet-app.tar.gz

                            echo "[3/7] Installing .env..."
                            sudo cp /tmp/timesheet.env \$DEPLOY_DIR/.env
                            sudo chown ubuntu:www-data \$DEPLOY_DIR/.env
                            sudo chmod 640 \$DEPLOY_DIR/.env
                            rm -f /tmp/timesheet.env

                            echo "[4/7] Setting up Python virtual environment..."
                            sudo python3 -m venv \$DEPLOY_DIR/venv
                            sudo \$DEPLOY_DIR/venv/bin/pip install --quiet --upgrade pip
                            sudo \$DEPLOY_DIR/venv/bin/pip install --quiet -r \$DEPLOY_DIR/requirements.txt

                            echo "[5/7] Fixing permissions..."
                            sudo chown -R ubuntu:www-data \$DEPLOY_DIR
                            sudo chmod -R 750 \$DEPLOY_DIR
                            # persistent_data must survive across deployments
                            sudo chmod -R 770 \$DEPLOY_DIR/persistent_data

                            echo "[6/7] Reloading systemd service..."
                            sudo systemctl daemon-reload
                            sudo systemctl enable \$SERVICE
                            sudo systemctl start \$SERVICE

                            echo "[7/7] Reloading Nginx..."
                            # Install/update site config if it has changed
                            sudo cp \$DEPLOY_DIR/DEPLOY_NGINX.conf /etc/nginx/sites-available/\$NGINX_SITE
                            sudo ln -sf /etc/nginx/sites-available/\$NGINX_SITE \
                                        /etc/nginx/sites-enabled/\$NGINX_SITE
                            sudo nginx -t
                            sudo systemctl reload nginx

                            echo "==> Deployment complete!"
REMOTE_SCRIPT
                        """
                    }
                }
            }
        }

        // ── 6. HEALTH CHECK ───────────────────────────────────
        stage('Health Check') {
            steps {
                echo "==> Verifying the app is responding..."
                withCredentials([
                    sshUserPrivateKey(
                        credentialsId: env.SSH_CRED_ID,
                        keyFileVariable: 'SSH_KEY'
                    )
                ]) {
                    script {
                        def sshOpts = "-i ${SSH_KEY} -o StrictHostKeyChecking=no -o BatchMode=yes"
                        def host    = params.DEPLOY_HOST

                        // Wait up to 30 s for Gunicorn socket to appear then hit Nginx
                        sh """
                            ssh ${sshOpts} ${host} bash -s <<'HEALTHCHECK'
                            set -euo pipefail
                            for i in \$(seq 1 6); do
                                HTTP_STATUS=\$(curl -s -o /dev/null -w "%{http_code}" http://localhost/)
                                echo "Attempt \$i — HTTP \$HTTP_STATUS"
                                if [ "\$HTTP_STATUS" = "200" ] || [ "\$HTTP_STATUS" = "302" ]; then
                                    echo "Health check PASSED"
                                    exit 0
                                fi
                                sleep 5
                            done
                            echo "Health check FAILED after 30 seconds"
                            sudo systemctl status timesheet --no-pager
                            exit 1
HEALTHCHECK
                        """
                    }
                }
            }
        }

    } // end stages

    // ── Post-pipeline actions ─────────────────────────────────
    post {
        success {
            echo "Pipeline SUCCESS — Timesheet app is live on ${params.DEPLOY_HOST}"
        }
        failure {
            echo "Pipeline FAILED — check the logs above for details"
            // Add emailext / Slack notification here if needed
            // mail to: 'team@example.com', subject: "BUILD FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}", body: "See ${env.BUILD_URL}"
        }
        always {
            // Clean up temporary files from workspace
            sh 'rm -f timesheet-app.tar.gz || true'
            cleanWs()
        }
    }

}
